"""Local streamable-HTTP MCP fixture on 127.0.0.1, synthetic data only.

The server is a protocol fixture, not a connection to any enterprise system. Its
modes let regression tests exercise redirect refusal, wrong media types,
compression, oversized payloads, truncated bodies, slow reads, authentication,
session headers and concurrent requests without leaving the loopback interface.
"""

import gzip
import json
import threading
import time
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOOL_SCHEMA = {
    "type": "object",
    "properties": {"metric": {"type": "string", "minLength": 1, "maxLength": 64}},
    "required": ["metric"],
    "additionalProperties": False,
}


def _catalog(version):
    return [
        {
            "name": "lookup_metric",
            "description": "Read one certified metric value.",
            "inputSchema": TOOL_SCHEMA,
        }
    ]


def _result(request_id, value):
    return {"jsonrpc": "2.0", "id": request_id, "result": value}


def _respond(request, *, mode, version):
    request_id = request.get("id")
    method = request.get("method")
    params = request.get("params") or {}
    if method == "initialize":
        return _result(
            request_id,
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "http-fixture", "version": version},
            },
        )
    if method == "tools/list":
        return _result(request_id, {"tools": _catalog(version)})
    if method == "tools/call":
        if mode == "error":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32603, "message": "fixture tool failed"},
            }
        names = {item["name"] for item in _catalog(version)}
        if params.get("name") not in names:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": "unknown tool"},
            }
        value = {
            "metric": (params.get("arguments") or {}).get("metric"),
            "period": "2026-Q1",
            "value": 1234,
            "source": "fixture://http-certified-metrics",
        }
        return _result(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(value, sort_keys=True)}],
                "structuredContent": value,
                "isError": False,
            },
        )
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": "unknown method"},
    }


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):  # keep test output quiet
        pass

    def do_POST(self):
        server = self.server
        if self.path != "/mcp":
            self._send(404, b"")
            return
        length = self.headers.get("content-length")
        if length is None or not length.isdigit() or int(length) > 1_048_576:
            self._send(413, b"")
            return
        body = self.rfile.read(int(length))
        token = server.token
        if token is not None and self.headers.get("authorization") != f"Bearer {token}":
            self._send(401, b'{"error":"unauthorized"}')
            return
        try:
            request = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, b'{"error":"bad json"}')
            return
        if not isinstance(request, dict):
            self._send(400, b'{"error":"bad frame"}')
            return
        with server.track():
            self._handle(server, request)

    def _handle(self, server, request):
        method = request.get("method")
        if (
            server.mode == "session"
            and method != "initialize"
            and (
                self.headers.get("mcp-session-id") != server.session_id
                or self.headers.get("mcp-protocol-version") != "2025-06-18"
            )
        ):
            self._send(400, b'{"error":"negotiated headers required"}')
            return
        if "id" not in request:
            self._send(202, b"")
            return
        if server.consume_failure(method):
            self.close_connection = True
            return
        if server.mode == "close_before_response":
            self.close_connection = True
            return
        if server.slow_method is not None and method == server.slow_method:
            time.sleep(server.delay)
        if server.mode == "disconnect":
            payload = json.dumps(_respond(request, mode="normal", version=server.version)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload) + 4096))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload[:128])
            self.close_connection = True
            return
        if server.mode == "eof":
            response = _respond(request, mode="normal", version=server.version)
            self.wfile.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Connection: close\r\n\r\n" + json.dumps(response).encode()
            )
            self.close_connection = True
            return
        if server.mode == "bad_status":
            self._raw(b"HTTP/1.1 20x Nope\r\nContent-Length: 0\r\n\r\n")
            return
        if server.mode == "bad_content_length":
            self._raw(b"HTTP/1.1 200 OK\r\nContent-Length: abc\r\n\r\n")
            return
        if server.mode == "folded_header":
            self._raw(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b" Content-Length: 2\r\nContent-Length: 0\r\n\r\n"
            )
            return
        if server.mode == "bad_transfer_encoding":
            self._raw(
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: gzip, chunked\r\n"
                b"Content-Type: application/json\r\n\r\n"
            )
            return
        if server.mode == "bad_chunk":
            self._raw(
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
                b"Content-Type: application/json\r\n\r\nzz\r\n"
            )
            return
        if server.mode == "bad_chunk_terminator":
            self._raw(
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
                b"Content-Type: application/json\r\n\r\n2\r\n{}XX"
            )
            return
        if server.mode == "ct_params":
            response = _respond(request, mode="normal", version=server.version)
            self._send(
                200,
                json.dumps(response).encode(),
                {"Content-Type": "application/json; charset=latin-1"},
            )
            return
        if server.mode == "empty_body":
            self._send(200, b"", {"Content-Type": "application/json"})
            return
        if server.mode == "bad_session":
            return self._send_json(server, request, {"Mcp-Session-Id": "bad id!"})
        if server.mode == "http_404":
            self._send(404, b"")
            return
        if server.mode == "event_type":
            stream = b'event: custom\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n\n'
            self._send(200, stream, {"Content-Type": "text/event-stream"})
            return
        if server.mode == "event_field":
            stream = b'foo: bar\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n\n'
            self._send(200, stream, {"Content-Type": "text/event-stream"})
            return
        if server.mode == "sse_empty":
            self._send(200, b": ping\n\n", {"Content-Type": "text/event-stream"})
            return
        if server.mode == "sse_bad_json":
            self._send(
                200,
                b"event: message\ndata: nope\n\n",
                {"Content-Type": "text/event-stream"},
            )
            return
        if server.mode == "session" and method == "initialize":
            return self._send_json(server, request, {"Mcp-Session-Id": server.session_id})
        if server.mode == "no_content" and method != "initialize":
            self._send(204, b"")
            return
        if server.mode == "server_error":
            self._send(500, b'{"error":"fixture failure"}')
            return
        if server.mode == "redirect":
            self._send(
                302,
                b"",
                {"Location": "http://127.0.0.1:9/elsewhere"},
            )
            return
        if server.mode == "wrong_id":
            response = _respond(request, mode="normal", version=server.version)
            response["id"] = response["id"] + 1
            self._send_json(server, None, body=response)
            return
        if server.mode == "oversized":
            response = _respond(request, mode="normal", version=server.version)
            response["result"]["padding"] = "x" * 8192
            self._send_json(server, None, body=response)
            return
        if server.mode == "oversized_chunked":
            self._send_chunked()
            return
        if server.mode == "wrong_type":
            response = _respond(request, mode="normal", version=server.version)
            self._send(
                200,
                json.dumps(response).encode(),
                {"Content-Type": "text/plain"},
            )
            return
        if server.mode == "gzip":
            response = _respond(request, mode="normal", version=server.version)
            self._send(
                200,
                gzip.compress(json.dumps(response).encode()),
                {"Content-Type": "application/json", "Content-Encoding": "gzip"},
            )
            return
        if server.mode == "bad_json":
            self._send(200, b"not-json", {"Content-Type": "application/json"})
            return
        if server.mode == "sse":
            response = _respond(request, mode="normal", version=server.version)
            progress = {
                "jsonrpc": "2.0",
                "method": "notifications/progress",
                "params": {"progress": 1},
            }
            stream = (
                "event: message\n"
                f"data: {json.dumps(progress)}\n\n"
                "event: message\n"
                f"data: {json.dumps(response)}\n\n"
            )
            self._send(200, stream.encode(), {"Content-Type": "text/event-stream"})
            return
        if server.mode == "sse_wrong_id":
            response = _respond(request, mode="normal", version=server.version)
            response["id"] = response["id"] + 1
            stream = f"event: message\ndata: {json.dumps(response)}\n\n"
            self._send(200, stream.encode(), {"Content-Type": "text/event-stream"})
            return
        self._send_json(server, request)

    def _raw(self, data):
        with suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(data)
        self.close_connection = True

    def _send_json(self, server, request, headers=None, body=None):
        payload = (
            body
            if body is not None
            else _respond(request, mode=server.mode, version=server.version)
        )
        extra = {"Content-Type": "application/json"}
        extra.update(headers or {})
        self._send(200, json.dumps(payload).encode(), extra)

    def _send_chunked(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()
        chunk = b"x" * 4096
        try:
            for _ in range(8):
                self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True

    def _send(self, status, body, headers=None):
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        with suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)
        self.close_connection = True


class FixtureServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self, address, handler, *, mode, token, version, delay, slow_method, fail_once_method
    ):
        super().__init__(address, handler)
        self.mode = mode
        self.token = token
        self.version = version
        self.delay = delay
        self.slow_method = slow_method
        self.fail_once_method = fail_once_method
        self.session_id = "sess-abc"
        self._lock = threading.Lock()
        self._failed_once = False
        self.inflight = 0
        self.max_inflight = 0
        self.requests = 0

    def consume_failure(self, method):
        with self._lock:
            if self.fail_once_method is None or self._failed_once:
                return False
            if method != self.fail_once_method:
                return False
            self._failed_once = True
            return True

    @contextmanager
    def track(self):
        with self._lock:
            self.requests += 1
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            yield
        finally:
            with self._lock:
                self.inflight -= 1


@contextmanager
def http_fixture(
    mode="normal",
    *,
    token=None,
    version="v1",
    delay=0.0,
    slow_method="tools/call",
    fail_once_method=None,
):
    server = FixtureServer(
        ("127.0.0.1", 0),
        _Handler,
        mode=mode,
        token=token,
        version=version,
        delay=delay,
        slow_method=slow_method,
        fail_once_method=fail_once_method,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        yield server, f"http://127.0.0.1:{port}/mcp"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
