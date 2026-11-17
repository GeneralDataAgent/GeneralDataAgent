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
    gateway_tool_name,
    mcp_description_hash,
    mcp_schema_hash,
    validate_mcp_schema,
)
from generaldataagent.security.dlp import DLPDeniedError, TextDLPScanner

SUPPORTED_PROTOCOL_VERSIONS = frozenset({"2025-06-18"})
_MAX_TOOLS = 64
_MAX_DESCRIPTION = 2000
_MAX_ERROR_TEXT = 500
_REMOTE_TOOL = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class McpError(RuntimeError):
    """Deterministic adapter failure with a stable reason code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail[:_MAX_ERROR_TEXT]


class McpServerUnavailable(McpError):
    pass


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
            try:
                frame = _canonical(
                    {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
                ).encode("utf-8")
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
    """One explicit MCP connection; timeout or cancellation closes it."""

    def __init__(self, profile: McpServerProfile, transport: McpTransport) -> None:
        self.profile = profile
        self.transport = transport
        self.server_name = ""
        self.server_version = ""
        self.protocol_version = ""
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
    ) -> AsyncIterator[McpSession]:
        scope = (tenant_id, project_id, user_id)
        async with self._lock:
            if self._active is not None:
                if self._scope is not None and self._scope[0] != tenant_id:
                    raise McpSessionScopeError("mcp_session_tenant_mismatch")
                if self._scope != scope:
                    raise McpSessionScopeError("mcp_session_scope_mismatch")
                raise McpSessionBusy("mcp_session_busy")
            session = McpSession(profile, self.transport_factory(profile, credential))
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
