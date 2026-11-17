"""Frozen contracts for one admitted, read-only MCP server.

Discovery can only produce candidates. Risk, roles and approval requirements
live in a local admission record; a remote tool description or annotation never
sets them. The schema hash covers the exact tool list, descriptions and input
schemas so a remote change forces a new admission.
"""

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from generaldataagent.contracts.models import ArtifactRef, ContractModel, utc_now
from generaldataagent.contracts.types import AgentRole, RiskLevel

_REMOTE_TOOL = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SERVER_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_SCHEMA_BYTES = 16_384
_MAX_SCHEMA_DEPTH = 16
_MAX_PROPERTIES = 64
_SCHEMA_KEYS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "description",
        "title",
        "default",
        "enum",
        "items",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
    }
)


def _canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def gateway_tool_name(tool_prefix: str, remote_tool: str) -> str:
    """Map one remote tool to the fixed local gateway namespace."""

    if not _REMOTE_TOOL.fullmatch(remote_tool):
        raise ValueError("mcp_remote_tool_invalid")
    return f"{tool_prefix}.{remote_tool.replace('-', '_')}"


def _validate_schema(schema: Any, *, depth: int = 0) -> None:
    """Accept only a small, deterministic JSON-schema subset.

    Unsupported validation keywords are rejected rather than ignored: a tool
    whose admitted schema says something this adapter cannot enforce must not
    be callable through it.
    """

    if depth > _MAX_SCHEMA_DEPTH:
        raise ValueError("mcp_schema_too_deep")
    if not isinstance(schema, dict):
        raise ValueError("mcp_schema_invalid")
    unknown = set(schema) - _SCHEMA_KEYS
    if unknown:
        raise ValueError("mcp_schema_keyword_unsupported")
    kind = schema.get("type", "object")
    if kind not in {"object", "string", "integer", "number", "boolean", "array"}:
        raise ValueError("mcp_schema_type_unsupported")
    if kind == "object":
        properties = schema.get("properties", {})
        if not isinstance(properties, dict) or len(properties) > _MAX_PROPERTIES:
            raise ValueError("mcp_schema_properties_invalid")
        for name, value in properties.items():
            if not isinstance(name, str) or not name or len(name) > 128:
                raise ValueError("mcp_schema_property_name_invalid")
            _validate_schema(value, depth=depth + 1)
        required = schema.get("required", [])
        if not isinstance(required, list) or any(
            not isinstance(name, str) or name not in properties for name in required
        ):
            raise ValueError("mcp_schema_required_invalid")
        if schema.get("additionalProperties", True) not in {True, False}:
            raise ValueError("mcp_schema_additional_properties_invalid")
    elif "properties" in schema or "required" in schema:
        raise ValueError("mcp_schema_properties_invalid")
    if kind == "array" and "items" in schema:
        _validate_schema(schema["items"], depth=depth + 1)
    enum = schema.get("enum")
    if enum is not None and (not isinstance(enum, list) or not enum or len(enum) > 100):
        raise ValueError("mcp_schema_enum_invalid")
    try:
        encoded = _canonical(schema)
    except (TypeError, ValueError) as exc:
        raise ValueError("mcp_schema_invalid") from exc
    if len(encoded.encode()) > _MAX_SCHEMA_BYTES:
        raise ValueError("mcp_schema_too_large")


def validate_mcp_schema(schema: Any) -> None:
    """Public validation hook for discovery and tools that pre-check a schema."""

    _validate_schema(schema)


def mcp_description_hash(description: str) -> str:
    digest = hashlib.sha256(description.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


class McpServerProfile(ContractModel):
    """Deployment-fixed server identity, transport and limits.

    The credential is named here and resolved from the process environment at
    use time; its value is never part of a contract.
    """

    server_id: str
    transport: Literal["stdio"] = "stdio"
    version: str = Field(pattern=r"^v[1-9][0-9]*$")
    tool_prefix: str = Field(pattern=r"^mcp\.[a-z][a-z0-9_]{0,31}$")
    timeout_seconds: float = Field(gt=0, le=60)
    max_response_bytes: int = Field(ge=1024, le=4 * 1024 * 1024)
    credential_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,63}$")

    @model_validator(mode="after")
    def fixed_namespace(self):
        if not _SERVER_ID.fullmatch(self.server_id):
            raise ValueError("mcp_server_id_invalid")
        if self.tool_prefix != f"mcp.{self.server_id}":
            raise ValueError("mcp_tool_prefix_mismatch")
        return self


class McpToolCandidate(ContractModel):
    """One remote tool exactly as discovered; it grants nothing."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    description: str = Field(max_length=2000)
    input_schema: dict[str, Any]
    description_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    input_schema_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def hashes_match(self):
        _validate_schema(self.input_schema)
        if self.description_sha256 != mcp_description_hash(self.description):
            raise ValueError("mcp_description_hash_mismatch")
        if self.input_schema_sha256 != _digest(_canonical(self.input_schema).encode("utf-8")):
            raise ValueError("mcp_schema_hash_mismatch")
        return self


class McpDiscovery(ContractModel):
    """A bounded tool-list observation; always a candidate set, never an admission."""

    server_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    version: str = Field(pattern=r"^v[1-9][0-9]*$")
    protocol_version: str = Field(min_length=1, max_length=32)
    schema_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    tools: tuple[McpToolCandidate, ...] = Field(min_length=1, max_length=64)
    discovered_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def digest_pinned(self):
        names = [tool.name for tool in self.tools]
        if len(names) != len(set(names)):
            raise ValueError("mcp_tool_conflict")
        expected = mcp_schema_hash(self.server_id, self.version, self.tools)
        if self.schema_sha256 != expected:
            raise ValueError("mcp_schema_hash_mismatch")
        return self


class McpToolAdmission(ContractModel):
    """Locally reviewed authority for exactly one discovered tool."""

    remote_tool: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    gateway_tool: str = Field(pattern=r"^mcp\.[a-z][a-z0-9_]{0,31}\.[a-z][a-z0-9_]{0,63}$")
    description_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    input_schema: dict[str, Any]
    risk_level: RiskLevel = RiskLevel.R1
    allowed_roles: tuple[AgentRole, ...] = Field(default=(AgentRole.VERIFIER,), min_length=1)
    action: Literal["read"] = "read"
    idempotent: Literal[True] = True

    @model_validator(mode="after")
    def read_only(self):
        if self.risk_level == RiskLevel.R4:
            raise ValueError("mcp_tool_permanently_denied")
        _validate_schema(self.input_schema)
        return self


class McpServerAdmission(ContractModel):
    """Pinned server profile, exact schema and locally reviewed tool decisions."""

    profile: McpServerProfile
    schema_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    tools: tuple[McpToolAdmission, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def exact_namespace(self):
        remote = [tool.remote_tool for tool in self.tools]
        gateway = [tool.gateway_tool for tool in self.tools]
        if len(remote) != len(set(remote)) or len(gateway) != len(set(gateway)):
            raise ValueError("mcp_tool_conflict")
        for tool in self.tools:
            if tool.gateway_tool != gateway_tool_name(self.profile.tool_prefix, tool.remote_tool):
                raise ValueError("mcp_tool_namespace_mismatch")
        expected = mcp_schema_hash(self.profile.server_id, self.profile.version, self.tools)
        if self.schema_sha256 != expected:
            raise ValueError("mcp_schema_hash_mismatch")
        return self

    def digest(self) -> str:
        return _digest(_canonical(self.model_dump(mode="json")).encode("utf-8"))


class McpReadRequest(ContractModel):
    """One explicit read through the product entry; arguments stay bounded."""

    task_id: UUID
    arguments: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def bounded_arguments(self):
        try:
            encoded = _canonical(self.arguments)
        except (TypeError, ValueError) as exc:
            raise ValueError("mcp_arguments_invalid") from exc
        if len(encoded.encode("utf-8")) > 65_536:
            raise ValueError("mcp_arguments_too_large")
        return self


class McpReadEvidence(ContractModel):
    """Provenance for one admitted read; content itself stays in the artifact."""

    task_id: UUID
    server_id: str
    server_version: str
    protocol_version: str
    remote_tool: str
    gateway_tool: str
    schema_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    admission_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    arguments_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    response_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    artifact: ArtifactRef
    approval_id: UUID | None = None
    read_at: datetime = Field(default_factory=utc_now)


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def mcp_schema_hash(
    server_id: str,
    version: str,
    tools: tuple[McpToolCandidate, ...] | tuple[McpToolAdmission, ...],
) -> str:
    payload = {
        "server_id": server_id,
        "version": version,
        "tools": [
            {
                "name": getattr(tool, "name", None) or tool.remote_tool,
                "description_sha256": tool.description_sha256,
                "input_schema": tool.input_schema,
            }
            for tool in sorted(
                tools, key=lambda item: getattr(item, "name", None) or item.remote_tool
            )
        ],
    }
    return _digest(_canonical(payload).encode("utf-8"))
