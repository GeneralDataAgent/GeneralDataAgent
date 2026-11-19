"""Bounded MCP protocol client, discovery and explicit session lifecycle.

This module speaks only newline-delimited JSON-RPC over an injected transport.
It never decides authorization: discovery returns candidates, and only a local
``McpServerAdmission`` can map one to a gateway tool. Remote descriptions and
annotations are treated as opaque text.
"""

import asyncio
import hashlib
import json
import os
import re
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from typing import Any, Protocol

from generaldataagent.contracts import McpDiscovery, McpServerProfile, McpToolCandidate
from generaldataagent.contracts.mcp import (
    LoopbackEndpoint,
    gateway_tool_name,
    mcp_description_hash,
    mcp_schema_hash,
    parse_loopback_endpoint,
    validate_mcp_schema,
)
from generaldataagent.security.dlp import DLPDeniedError, TextDLPScanner

SUPPORTED_PROTOCOL_VERSIONS = frozenset({"2025-06-18"})
_MAX_TOOLS = 64
_MAX_DESCRIPTION = 2000
_MAX_ERROR_TEXT = 500
_MAX_HTTP_LINE_BYTES = 8_192
_MAX_HTTP_HEAD_BYTES = 16_384
_MAX_HTTP_HEADERS = 64
_MAX_SSE_EVENTS = 64
_MAX_REQUEST_BYTES = 262_144
_MAX_TRANSPORT_ATTEMPTS = 2
_RESPONSE_MEDIA_TYPES = frozenset({"application/json", "text/event-stream"})
_IDENTITY_ENCODINGS = frozenset({"", "identity"})
_SESSION_ID = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9a-z-]{1,64}$")
_CREDENTIAL = re.compile(r"^[\x21-\x7e]{1,4096}$")
_REMOTE_TOOL = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class McpError(RuntimeError):
    """Deterministic adapter failure with a stable reason code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail[:_MAX_ERROR_TEXT]


class McpServerUnavailable(McpError):
    """The server could not complete the exchange.

    ``retry_safe`` is true only when no response byte was received: the
    connection failed, the write failed, or the connection closed before the
    status line. Only an idempotent read may be retried in that case; a failure
    after any response byte, an HTTP error status, authentication, protocol,
    size, DLP or timeout failure is never retried.
    """

    def __init__(self, code: str, detail: str = "", *, retry_safe: bool = False) -> None:
        super().__init__(code, detail)
        self.retry_safe = retry_safe


class McpAuthFailed(McpError):
    pass


class McpProtocolError(McpError):
    pass


class McpToolError(McpError):
    pass


class McpToolNotFound(McpError):
    pass


class McpTimeout(McpError):
    pass


class McpResponseTooLarge(McpError):
    pass


class McpResponseDenied(McpError):
    pass


class McpSchemaDrift(McpError):
    pass


class McpToolConflict(McpError):
    pass


class McpVersionMismatch(McpError):
    pass


class McpRequestRejected(McpError):
    pass


class McpSessionScopeError(McpError):
    pass


class McpSessionBusy(McpError):
    pass


class McpToolNotAdmitted(McpError):
    pass


def _canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def resolve_credential(profile: McpServerProfile) -> str | None:
    """Read the single fixed credential source at use time, never from a contract."""

    if profile.credential_env is None:
        return None
    value = os.environ.get(profile.credential_env)
    if not value:
        raise McpAuthFailed("mcp_credential_unavailable")
    return value


def validate_mcp_arguments(schema: dict[str, Any], arguments: Any) -> None:
    """Validate arguments against the admitted subset without remote evaluation."""

    if not isinstance(arguments, dict):
        raise McpRequestRejected("mcp_arguments_not_object")
    kind = schema.get("type", "object")
    if kind != "object":
        raise McpRequestRejected("mcp_arguments_schema_not_object")
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    missing = [name for name in required if name not in arguments]
    if missing:
        raise McpRequestRejected("mcp_arguments_required")
    if schema.get("additionalProperties", True) is False and set(arguments) - set(properties):
        raise McpRequestRejected("mcp_arguments_unknown")
    for name, value in arguments.items():
        spec = properties.get(name)
        if spec is None:
            continue
        _validate_value(value, spec)


def _validate_value(value: Any, spec: dict[str, Any]) -> None:
    kind = spec.get("type", "object")
    checks = {
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: type(item) is int,
        "number": lambda item: type(item) in {int, float},
        "boolean": lambda item: type(item) is bool,
        "array": lambda item: isinstance(item, list),
        "object": lambda item: isinstance(item, dict),
    }
    if kind in checks and not checks[kind](value):
        raise McpRequestRejected("mcp_arguments_type")
    if kind == "string":
        if "minLength" in spec and len(value) < spec["minLength"]:
            raise McpRequestRejected("mcp_arguments_length")
        if "maxLength" in spec and len(value) > spec["maxLength"]:
            raise McpRequestRejected("mcp_arguments_length")
    if kind in {"integer", "number"}:
        if "minimum" in spec and value < spec["minimum"]:
            raise McpRequestRejected("mcp_arguments_range")
        if "maximum" in spec and value > spec["maximum"]:
            raise McpRequestRejected("mcp_arguments_range")
    if kind == "array" and isinstance(spec.get("items"), dict):
        for item in value:
            _validate_value(item, spec["items"])
    if kind == "object":
        validate_mcp_arguments(spec, value)
    if "enum" in spec and value not in spec["enum"]:
        raise McpRequestRejected("mcp_arguments_enum")


def _scan_text(text: str, scanner: TextDLPScanner) -> None:
    try:
        scanner.check(text.encode("utf-8"))
    except DLPDeniedError as exc:
        raise McpResponseDenied("mcp_content_denied", str(exc)) from exc


def _candidate(tool: Any, scanner: TextDLPScanner) -> McpToolCandidate:
    if not isinstance(tool, dict):
        raise McpProtocolError("mcp_tool_shape_invalid")
    name = tool.get("name")
    if not isinstance(name, str) or not _REMOTE_TOOL.fullmatch(name):
        raise McpProtocolError("mcp_tool_name_invalid")
    description = tool.get("description", "")
    if not isinstance(description, str):
        raise McpProtocolError("mcp_tool_description_invalid")
    if len(description) > _MAX_DESCRIPTION:
        raise McpResponseTooLarge("mcp_tool_description_too_large")
    schema = tool.get("inputSchema", {})
    try:
        validate_mcp_schema(schema)
        canonical_schema = _canonical(schema)
    except (TypeError, ValueError) as exc:
        raise McpProtocolError("mcp_tool_schema_invalid", str(exc)) from exc
    _scan_text(description, scanner)
    _scan_text(canonical_schema, scanner)
    return McpToolCandidate(
        name=name,
        description=description,
        input_schema=schema,
        description_sha256=mcp_description_hash(description),
        input_schema_sha256=_digest(canonical_schema.encode("utf-8")),
    )


def build_discovery(
    profile: McpServerProfile,
    protocol_version: str,
    tools: Any,
    *,
    scanner: TextDLPScanner | None = None,
) -> McpDiscovery:
    """Convert one remote tool list into bounded, hash-pinned candidates."""

    scanner = scanner or TextDLPScanner()
    if not isinstance(tools, list) or not tools or len(tools) > _MAX_TOOLS:
        raise McpProtocolError("mcp_tool_list_invalid")
    candidates = tuple(_candidate(tool, scanner) for tool in tools)
    names = [gateway_tool_name(profile.tool_prefix, tool.name) for tool in candidates]
    if len(names) != len(set(names)):
        raise McpToolConflict("mcp_tool_name_conflict")
    return McpDiscovery(
        server_id=profile.server_id,
        version=profile.version,
        protocol_version=protocol_version,
        schema_sha256=mcp_schema_hash(profile.server_id, profile.version, candidates),
        tools=candidates,
    )


class McpTransport(Protocol):
    async def start(self) -> None: ...

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]: ...

    async def notify(self, method: str) -> None: ...

    async def close(self) -> None: ...


class StdioMcpTransport:
    """Newline-delimited JSON-RPC over one repository-supplied child process.

    The command is deployment configuration, never model output. Every response
    line is bounded before decoding; a protocol, size or process failure closes
    the child and is surfaced as a deterministic adapter error.
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        max_line_bytes: int = 1_048_576,
    ) -> None:
        if not command or any(not isinstance(part, str) or not part for part in command):
            raise ValueError("mcp_command_invalid")
        if type(max_line_bytes) is not int or max_line_bytes < 1024:
            raise ValueError("mcp_line_limit_invalid")
        self.command = tuple(command)
        self.env = None if env is None else dict(env)
        self.max_line_bytes = max_line_bytes
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._count = 0
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    async def start(self) -> None:
        if not self._closed:
            raise McpProtocolError("mcp_transport_already_started")
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=self.env,
                limit=self.max_line_bytes + 1,
            )
        except OSError as exc:
            raise McpServerUnavailable("mcp_server_unavailable", str(exc)) from exc
        self._closed = False

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            if self._closed or self._process is None or self._process.stdin is None:
                raise McpProtocolError("mcp_transport_closed")
            self._count += 1
            request_id = self._count
            await self._write_locked(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
            try:
                line = await self._process.stdout.readline()
            except (asyncio.LimitOverrunError, ValueError) as exc:
                await self._close_locked()
                raise McpResponseTooLarge("mcp_response_too_large", str(exc)) from exc
            except (ConnectionResetError, BrokenPipeError) as exc:
                await self._close_locked()
                raise McpServerUnavailable("mcp_server_closed", str(exc)) from exc
            if not line:
                await self._close_locked()
                raise McpServerUnavailable("mcp_server_closed")
            if len(line) > self.max_line_bytes:
                await self._close_locked()
                raise McpResponseTooLarge("mcp_response_too_large")
            try:
                response = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                await self._close_locked()
                raise McpProtocolError("mcp_response_invalid", str(exc)) from exc
            if not isinstance(response, dict) or response.get("id") != request_id:
                await self._close_locked()
                raise McpProtocolError("mcp_response_mismatched")
            return response

    async def notify(self, method: str) -> None:
        async with self._lock:
            await self._write_locked({"jsonrpc": "2.0", "method": method})

    async def _write_locked(self, message: dict[str, Any]) -> None:
        if self._closed or self._process is None or self._process.stdin is None:
            raise McpProtocolError("mcp_transport_closed")
        try:
            frame = _canonical(message).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise McpRequestRejected("mcp_request_not_serializable", str(exc)) from exc
        if len(frame) > self.max_line_bytes:
            raise McpRequestRejected("mcp_request_too_large")
        try:
            self._process.stdin.write(frame + b"\n")
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            await self._close_locked()
            raise McpServerUnavailable("mcp_server_closed", str(exc)) from exc

    async def _close_locked(self) -> None:
        process = self._process
        self._process, self._closed = None, True
        if process is None:
            return
        with suppress(ProcessLookupError, RuntimeError):
            if process.stdin is not None:
                process.stdin.close()
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=1)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    process.kill()
                with suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=1)

    async def close(self) -> None:
        async with self._lock:
            await self._close_locked()


class HttpMcpTransport:
    """Streamable HTTP JSON-RPC over one validated loopback endpoint.

    One request is in flight at a time, each request uses its own connection and
    only the two MCP media types are accepted. Redirects, proxies, compressed
    bodies and oversized frames are refused before any payload is decoded. The
    session owner enforces the deadline; cancellation or ``close`` closes the
    socket under the in-flight request so a stalled read cannot outlive it.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        credential: str | None = None,
        max_response_bytes: int = 1_048_576,
    ) -> None:
        self._endpoint: LoopbackEndpoint = parse_loopback_endpoint(endpoint)
        if type(max_response_bytes) is not int or max_response_bytes < 1024:
            raise ValueError("mcp_response_limit_invalid")
        if credential is not None and (
            not isinstance(credential, str) or not _CREDENTIAL.fullmatch(credential)
        ):
            raise ValueError("mcp_credential_invalid")
        self.endpoint = endpoint
        self.max_response_bytes = max_response_bytes
        self._credential = credential
        self._session_id: str | None = None
        self._protocol_version: str | None = None
        self._lock = asyncio.Lock()
        self._count = 0
        self._writer: asyncio.StreamWriter | None = None
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    async def start(self) -> None:
        if not self._closed:
            raise McpProtocolError("mcp_transport_already_started")
        self._session_id = None
        self._protocol_version = None
        self._count = 0
        self._closed = False

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            if self._closed:
                raise McpProtocolError("mcp_transport_closed")
            self._count += 1
            request_id = self._count
            message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            response = await self._exchange(message)
            if not isinstance(response, dict) or response.get("id") != request_id:
                raise McpProtocolError("mcp_response_mismatched")
            return response

    async def notify(self, method: str) -> None:
        async with self._lock:
            if self._closed:
                raise McpProtocolError("mcp_transport_closed")
            await self._exchange({"jsonrpc": "2.0", "method": method})

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            writer, self._writer = self._writer, None
            if writer is not None:
                writer.close()
                with suppress(Exception, asyncio.CancelledError):
                    await asyncio.wait_for(writer.wait_closed(), timeout=1)

    async def _exchange(self, message: dict[str, Any]) -> Any:
        try:
            body = _canonical(message).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise McpRequestRejected("mcp_request_not_serializable", str(exc)) from exc
        if len(body) > _MAX_REQUEST_BYTES:
            raise McpRequestRejected("mcp_request_too_large")
        request_id = message.get("id")
        head = [
            f"POST {self._endpoint.path} HTTP/1.1",
            f"Host: {self._endpoint.host_header}",
            "Content-Type: application/json",
            "Accept: application/json, text/event-stream",
            f"Content-Length: {len(body)}",
            "Connection: close",
        ]
        if self._credential is not None:
            head.append(f"Authorization: Bearer {self._credential}")
        if request_id is not None and self._session_id is not None:
            head.append(f"Mcp-Session-Id: {self._session_id}")
        if request_id is not None and self._protocol_version is not None:
            head.append(f"MCP-Protocol-Version: {self._protocol_version}")
        frame = ("\r\n".join(head) + "\r\n\r\n").encode("ascii") + body

        reader, writer = await self._connect()
        try:
            try:
                writer.write(frame)
                await writer.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise McpServerUnavailable("mcp_server_closed", str(exc), retry_safe=True) from exc
            status, headers = await self._read_head(reader)
            self._check_status(status)
            if request_id is None:
                await self._discard_body(reader, headers)
                return {}
            if status != 200:
                raise McpProtocolError("mcp_response_missing", f"http_{status}")
            if message.get("method") == "initialize":
                self._remember(headers, message.get("params"))
            encoding = headers.get("content-encoding", "").strip().lower()
            if encoding not in _IDENTITY_ENCODINGS:
                raise McpProtocolError("mcp_content_encoding_invalid")
            media_type = _media_type(headers.get("content-type"))
            payload = await self._read_body(reader, headers)
            if media_type == "text/event-stream":
                return _parse_sse(payload, request_id)
            return _parse_json(payload)
        finally:
            await _release_writer(writer)

    def _remember(self, headers: Mapping[str, str], params: Any) -> None:
        session_id = headers.get("mcp-session-id")
        if session_id is not None:
            if not _SESSION_ID.fullmatch(session_id):
                raise McpProtocolError("mcp_session_id_invalid")
            self._session_id = session_id
        if isinstance(params, dict):
            version = params.get("protocolVersion")
            if isinstance(version, str) and 0 < len(version) <= 32:
                self._protocol_version = version

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        try:
            reader, writer = await asyncio.open_connection(
                self._endpoint.host,
                self._endpoint.port,
                limit=_MAX_HTTP_LINE_BYTES + 1,
            )
        except OSError as exc:
            raise McpServerUnavailable("mcp_server_unavailable", str(exc), retry_safe=True) from exc
        if self._closed:
            await _release_writer(writer)
            raise McpProtocolError("mcp_transport_closed")
        self._writer = writer
        return reader, writer

    @staticmethod
    def _check_status(status: int) -> None:
        if 300 <= status < 400:
            raise McpServerUnavailable("mcp_redirect_refused", f"http_{status}")
        if status in {401, 403}:
            raise McpAuthFailed("mcp_auth_failed", f"http_{status}")
        if status == 429 or 500 <= status < 600:
            raise McpServerUnavailable("mcp_server_unavailable", f"http_{status}")
        if status not in {200, 202, 204}:
            raise McpProtocolError("mcp_http_status", f"http_{status}")

    async def _read_head(self, reader: asyncio.StreamReader) -> tuple[int, dict[str, str]]:
        line = await _readline(reader, retry_safe_empty=True)
        try:
            version, status_text, *_rest = line.decode("latin-1").rstrip("\r\n").split(" ")
            if (
                not version.startswith("HTTP/1.")
                or not status_text.isdigit()
                or len(status_text) != 3
            ):
                raise ValueError
            status = int(status_text)
        except (UnicodeDecodeError, ValueError) as exc:
            raise McpProtocolError("mcp_http_status_line_invalid") from exc
        headers: dict[str, str] = {}
        total = len(line)
        for _ in range(_MAX_HTTP_HEADERS):
            raw = await _readline(reader)
            total += len(raw)
            if total > _MAX_HTTP_HEAD_BYTES:
                raise McpProtocolError("mcp_http_headers_too_large")
            if raw in {b"\r\n", b"\n"}:
                return status, headers
            if raw[:1] in {b" ", b"\t"}:
                raise McpProtocolError("mcp_http_header_folded")
            name, _, value = raw.decode("latin-1").partition(":")
            name = name.strip().lower()
            value = value.strip()
            if not _HEADER_NAME.fullmatch(name) or any(char in value for char in "\r\n\x00"):
                raise McpProtocolError("mcp_http_header_invalid")
            if name == "content-length":
                if not value.isdigit() or (name in headers and headers[name] != value):
                    raise McpProtocolError("mcp_http_content_length_invalid")
                headers[name] = value
                continue
            headers[name] = value if name not in headers else f"{headers[name]}, {value}"
        raise McpProtocolError("mcp_http_headers_too_many")

    async def _read_body(self, reader: asyncio.StreamReader, headers: Mapping[str, str]) -> bytes:
        transfer = headers.get("transfer-encoding", "").lower()
        if transfer:
            codings = [part.strip() for part in transfer.split(",") if part.strip()]
            if codings != ["chunked"]:
                raise McpProtocolError("mcp_transfer_encoding_invalid")
            return await self._read_chunked(reader)
        length = headers.get("content-length")
        if length is None:
            return await self._read_until_eof(reader)
        if not length.isdigit():
            raise McpProtocolError("mcp_http_content_length_invalid")
        size = int(length)
        if size > self.max_response_bytes:
            raise McpResponseTooLarge("mcp_response_too_large")
        return await self._read_exact(reader, size)

    async def _discard_body(self, reader: asyncio.StreamReader, headers: Mapping[str, str]) -> None:
        if headers.get("transfer-encoding") or "content-length" in headers:
            await self._read_body(reader, headers)

    async def _read_exact(self, reader: asyncio.StreamReader, size: int) -> bytes:
        try:
            return await reader.readexactly(size)
        except asyncio.IncompleteReadError as exc:
            raise McpServerUnavailable("mcp_server_closed", str(exc)) from exc
        except (ConnectionResetError, BrokenPipeError) as exc:
            raise McpServerUnavailable("mcp_server_closed", str(exc)) from exc

    async def _read_until_eof(self, reader: asyncio.StreamReader) -> bytes:
        chunks = bytearray()
        try:
            while True:
                chunk = await reader.read(65_536)
                if not chunk:
                    return bytes(chunks)
                chunks.extend(chunk)
                if len(chunks) > self.max_response_bytes:
                    raise McpResponseTooLarge("mcp_response_too_large")
        except (ConnectionResetError, BrokenPipeError) as exc:
            raise McpServerUnavailable("mcp_server_closed", str(exc)) from exc

    async def _read_chunked(self, reader: asyncio.StreamReader) -> bytes:
        chunks = bytearray()
        while True:
            line = await _readline(reader)
            try:
                size = int(line.split(b";", 1)[0].strip(), 16)
            except ValueError as exc:
                raise McpProtocolError("mcp_chunk_size_invalid", str(exc)) from exc
            if size < 0:
                raise McpProtocolError("mcp_chunk_size_invalid")
            if size == 0:
                for _ in range(_MAX_HTTP_HEADERS):
                    if (await _readline(reader)) in {b"\r\n", b"\n"}:
                        return bytes(chunks)
                raise McpProtocolError("mcp_chunk_trailer_invalid")
            if len(chunks) + size > self.max_response_bytes:
                raise McpResponseTooLarge("mcp_response_too_large")
            chunks.extend(await self._read_exact(reader, size))
            terminator = await _readline(reader)
            if terminator not in {b"\r\n", b"\n"}:
                raise McpProtocolError("mcp_chunk_terminator_invalid")


def http_transport_factory(profile: McpServerProfile, credential: str | None) -> HttpMcpTransport:
    """Build the loopback HTTP transport fixed by one server profile."""

    if profile.transport != "http" or profile.endpoint is None:
        raise McpProtocolError("mcp_http_profile_required")
    return HttpMcpTransport(
        profile.endpoint,
        credential=credential,
        max_response_bytes=profile.max_response_bytes,
    )


async def _release_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with suppress(Exception, asyncio.CancelledError):
        await asyncio.wait_for(writer.wait_closed(), timeout=1)


async def _readline(reader: asyncio.StreamReader, *, retry_safe_empty: bool = False) -> bytes:
    try:
        raw = await reader.readline()
    except (ValueError, asyncio.LimitOverrunError) as exc:
        raise McpProtocolError("mcp_http_line_too_long", str(exc)) from exc
    except (ConnectionResetError, BrokenPipeError) as exc:
        raise McpServerUnavailable("mcp_server_closed", str(exc)) from exc
    if not raw:
        raise McpServerUnavailable("mcp_server_closed", retry_safe=retry_safe_empty)
    if len(raw) > _MAX_HTTP_LINE_BYTES:
        raise McpProtocolError("mcp_http_line_too_long")
    return raw


def _media_type(value: str | None) -> str:
    if not value:
        raise McpProtocolError("mcp_content_type_invalid")
    parts = [part.strip() for part in value.split(";")]
    media = parts[0].lower()
    if media not in _RESPONSE_MEDIA_TYPES:
        raise McpProtocolError("mcp_content_type_invalid", media[:64])
    for parameter in parts[1:]:
        if parameter.lower() not in {"charset=utf-8", 'charset="utf-8"'}:
            raise McpProtocolError("mcp_content_type_invalid", parameter[:64])
    return media


def _parse_json(payload: bytes) -> dict[str, Any]:
    if not payload:
        raise McpProtocolError("mcp_response_invalid")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise McpProtocolError("mcp_response_invalid", str(exc)) from exc
    if not isinstance(value, dict):
        raise McpProtocolError("mcp_response_invalid")
    return value


def _parse_sse(payload: bytes, request_id: int) -> dict[str, Any]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise McpProtocolError("mcp_response_invalid", str(exc)) from exc
    events: list[tuple[str, list[str]]] = []
    event, data = "message", []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw == "":
            if data:
                if len(events) >= _MAX_SSE_EVENTS:
                    raise McpProtocolError("mcp_event_count_invalid")
                events.append((event, data))
            event, data = "message", []
            continue
        if raw.startswith(":"):
            continue
        field, _, value = raw.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event = value
        elif field == "data":
            data.append(value)
        elif field not in {"id", "retry"}:
            raise McpProtocolError("mcp_event_invalid")
    if data:
        if len(events) >= _MAX_SSE_EVENTS:
            raise McpProtocolError("mcp_event_count_invalid")
        events.append((event, data))
    if not events or len(events) > _MAX_SSE_EVENTS:
        raise McpProtocolError("mcp_event_count_invalid")
    for name, lines in events:
        if name != "message":
            raise McpProtocolError("mcp_event_type_invalid", name[:64])
        try:
            message = json.loads("\n".join(lines))
        except json.JSONDecodeError as exc:
            raise McpProtocolError("mcp_response_invalid", str(exc)) from exc
        if not isinstance(message, dict):
            raise McpProtocolError("mcp_response_invalid")
        if "id" not in message:
            if "method" in message:
                continue
            raise McpProtocolError("mcp_response_invalid")
        if message.get("id") != request_id:
            raise McpProtocolError("mcp_response_mismatched")
        return message
    raise McpProtocolError("mcp_response_missing")


def _error(response: dict[str, Any]) -> McpError:
    error = response.get("error")
    if not isinstance(error, dict):
        return McpProtocolError("mcp_response_error_invalid")
    code = error.get("code")
    detail = str(error.get("message", ""))[:_MAX_ERROR_TEXT]
    if code == -32001:
        return McpAuthFailed("mcp_auth_failed", detail)
    if code == -32601:
        return McpToolNotFound("mcp_tool_not_found", detail)
    if code == -32602:
        return McpRequestRejected("mcp_request_invalid", detail)
    return McpToolError("mcp_tool_error", detail)


class McpSession:
    """One explicit MCP connection; timeout or cancellation closes it.

    A read that fails before any response byte was received may be attempted
    once more. The retry is only for the transport-level unavailable case; the
    caller supplies an optional notice callback so every attempt can be audited
    and charged to the task budget.
    """

    def __init__(
        self,
        profile: McpServerProfile,
        transport: McpTransport,
        *,
        retry_notice: Callable[[str], None] | None = None,
    ) -> None:
        self.profile = profile
        self.transport = transport
        self.server_name = ""
        self.server_version = ""
        self.protocol_version = ""
        self.retry_count = 0
        self._retry_notice = retry_notice
        self._started = False
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    async def __aenter__(self) -> "McpSession":
        await self.start()
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()

    async def start(self) -> None:
        if self._started or self._closed:
            raise McpProtocolError("mcp_session_already_started")
        self._started = True
        self.retry_count = 0
        try:
            await self.transport.start()
            result = await self._request(
                "initialize",
                {
                    "protocolVersion": sorted(SUPPORTED_PROTOCOL_VERSIONS)[-1],
                    "capabilities": {},
                    "clientInfo": {"name": "generaldataagent", "version": "1.0"},
                },
            )
            if not isinstance(result, dict):
                raise McpProtocolError("mcp_initialize_invalid")
            protocol = result.get("protocolVersion")
            if protocol not in SUPPORTED_PROTOCOL_VERSIONS:
                raise McpProtocolError("mcp_protocol_unsupported")
            info = result.get("serverInfo")
            if not isinstance(info, dict) or not isinstance(info.get("version"), str):
                raise McpProtocolError("mcp_server_info_invalid")
            if info["version"] != self.profile.version:
                raise McpVersionMismatch("mcp_version_mismatch")
            try:
                await asyncio.wait_for(
                    self.transport.notify("notifications/initialized"),
                    timeout=self.profile.timeout_seconds,
                )
            except TimeoutError as exc:
                raise McpTimeout("mcp_timeout") from exc
            self.server_name = str(info.get("name", ""))[:128]
            self.server_version = info["version"]
            self.protocol_version = protocol
        except BaseException:
            await self.close()
            raise

    async def list_tools(self) -> list[Any]:
        result = await self._request("tools/list", {})
        tools = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(tools, list):
            raise McpProtocolError("mcp_tool_list_invalid")
        return tools

    async def call_tool(self, remote_tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self._request("tools/call", {"name": remote_tool, "arguments": arguments})
        if not isinstance(result, dict):
            raise McpProtocolError("mcp_tool_result_invalid")
        if result.get("isError") is True:
            content = result.get("content")
            detail = ""
            if isinstance(content, list):
                texts = [
                    item.get("text")
                    for item in content
                    if isinstance(item, dict) and isinstance(item.get("text"), str)
                ]
                detail = " ".join(texts)[:_MAX_ERROR_TEXT]
            raise McpToolError("mcp_tool_error", detail)
        return result

    async def _request(self, method: str, params: dict[str, Any]) -> Any:
        if self._closed or not self._started:
            raise McpProtocolError("mcp_session_closed")
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await asyncio.wait_for(
                    self.transport.request(method, params),
                    timeout=self.profile.timeout_seconds,
                )
            except TimeoutError as exc:
                await self.close()
                raise McpTimeout("mcp_timeout") from exc
            except asyncio.CancelledError:
                await self.close()
                raise
            except McpServerUnavailable as exc:
                if attempt >= _MAX_TRANSPORT_ATTEMPTS or not exc.retry_safe or self._closed:
                    raise
                self.retry_count += 1
                if self._retry_notice is not None:
                    self._retry_notice(exc.code)
                continue
            break
        if "error" in response:
            raise _error(response)
        if "result" not in response:
            raise McpProtocolError("mcp_response_missing_result")
        return response["result"]

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._started:
            await self.transport.close()


async def discover_tools(
    profile: McpServerProfile,
    *,
    transport_factory: Callable[[McpServerProfile, str | None], McpTransport],
    scanner: TextDLPScanner | None = None,
) -> McpDiscovery:
    """Open one bounded session, read the tool list and close it."""

    credential = resolve_credential(profile)
    transport = transport_factory(profile, credential)
    async with McpSession(profile, transport) as session:
        tools = await session.list_tools()
        return build_discovery(
            profile, session.protocol_version, tools, scanner=scanner or TextDLPScanner()
        )


class McpSessionManager:
    """At most one active session per server, bound to one actor scope.

    A second caller with a different tenant is refused rather than being given
    the existing session; a remote session is never shared across tenants.
    """

    def __init__(
        self, transport_factory: Callable[[McpServerProfile, str | None], McpTransport]
    ) -> None:
        self.transport_factory = transport_factory
        self._active: McpSession | None = None
        self._scope: tuple[str, str, str] | None = None
        self._lock = asyncio.Lock()

    @property
    def active(self) -> McpSession | None:
        return self._active

    @asynccontextmanager
    async def lease(
        self,
        profile: McpServerProfile,
        credential: str | None,
        *,
        tenant_id: str,
        project_id: str,
        user_id: str,
        retry_notice: Callable[[str], None] | None = None,
    ) -> AsyncIterator[McpSession]:
        scope = (tenant_id, project_id, user_id)
        async with self._lock:
            if self._active is not None:
                if self._scope is not None and self._scope[0] != tenant_id:
                    raise McpSessionScopeError("mcp_session_tenant_mismatch")
                if self._scope != scope:
                    raise McpSessionScopeError("mcp_session_scope_mismatch")
                raise McpSessionBusy("mcp_session_busy")
            session = McpSession(
                profile,
                self.transport_factory(profile, credential),
                retry_notice=retry_notice,
            )
            try:
                await session.start()
            except BaseException:
                await session.close()
                raise
            self._active, self._scope = session, scope
        try:
            yield session
        finally:
            async with self._lock:
                if self._active is session:
                    self._active, self._scope = None, None
            await session.close()

    async def aclose(self) -> None:
        async with self._lock:
            session, self._active, self._scope = self._active, None, None
        if session is not None:
            await session.close()
