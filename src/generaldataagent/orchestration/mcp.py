"""Governed read-only MCP runtime: admission, gateway mapping and evidence.

The model-facing tool name is always local (``mcp.<server>.<tool>``). A remote
server contributes candidates and content only; risk, roles and approval
requirements come from ``McpServerAdmission``. Reads run through ``ToolGateway``
and every returned payload becomes a creator-bound artifact before evidence is
returned.
"""

import asyncio
import hashlib
import inspect
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from generaldataagent.artifacts.local import LocalArtifactStore
from generaldataagent.business.extensions import ExtensionAdmission
from generaldataagent.business.governance import CapabilityRegistry
from generaldataagent.connectors.mcp import (
    McpResponseDenied,
    McpResponseTooLarge,
    McpSchemaDrift,
    McpSessionManager,
    McpToolNotAdmitted,
    McpTransport,
    build_discovery,
    resolve_credential,
    validate_mcp_arguments,
)
from generaldataagent.contracts import (
    ActorContext,
    AgentRole,
    AuditEvent,
    McpDiscovery,
    McpReadEvidence,
    McpServerAdmission,
    McpServerProfile,
    McpToolAdmission,
    RiskLevel,
    ToolRequest,
)
from generaldataagent.contracts.mcp import _canonical
from generaldataagent.observability.audit import JsonlAuditSink
from generaldataagent.policy import ApprovalStore, PolicyEngine, PolicyRule
from generaldataagent.policy.budgets import TaskBudgetLedger
from generaldataagent.policy.engine import DEFAULT_RULES
from generaldataagent.security.dlp import DLPDeniedError, TextDLPScanner
from generaldataagent.tools.gateway import ToolGateway

_ARTIFACT_TYPE = "mcp_read_result"


class McpReadService:
    """One admitted server and its single read path through ``ToolGateway``."""

    def __init__(
        self,
        *,
        profile: McpServerProfile,
        admission: McpServerAdmission,
        registry: CapabilityRegistry,
        store: LocalArtifactStore,
        transport_factory: Callable[[McpServerProfile, str | None], McpTransport],
        audit: JsonlAuditSink | None = None,
        scanner: TextDLPScanner | None = None,
        approvals: ApprovalStore | None = None,
        budgets: TaskBudgetLedger | None = None,
        alerts: Any | None = None,
        agent_role: AgentRole = AgentRole.VERIFIER,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(profile, McpServerProfile):
            raise ValueError("mcp_profile_required")
        if not isinstance(admission, McpServerAdmission) or admission.profile != profile:
            raise ValueError("mcp_admission_profile_mismatch")
        if not isinstance(registry, CapabilityRegistry):
            raise ValueError("mcp_admission_registry_required")
        if not isinstance(store, LocalArtifactStore):
            raise ValueError("mcp_artifact_store_required")
        if not callable(transport_factory):
            raise ValueError("mcp_transport_factory_required")
        try:
            role = AgentRole(agent_role)
        except (TypeError, ValueError):
            raise ValueError("mcp_agent_role_invalid") from None
        self.profile = profile
        self.admission = admission
        self.admission_hash = admission.digest()
        self.registry = registry
        self.store = store
        self.audit = audit
        self.scanner = scanner or TextDLPScanner()
        self.agent_role = role
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sessions = McpSessionManager(transport_factory)
        self._tools = {tool.remote_tool: tool for tool in admission.tools}
        rules = (*DEFAULT_RULES, *self._policy_rules())
        names = [rule.tool_name for rule in rules]
        if len(names) != len(set(names)):
            raise ValueError("mcp_tool_name_conflict")
        self.gateway = ToolGateway(
            PolicyEngine(rules),
            audit,
            approvals,
            budgets=budgets,
            alerts=alerts,
        )
        for tool in admission.tools:
            self.gateway.register(tool.gateway_tool, self._handler(tool))

    def _policy_rules(self) -> tuple[PolicyRule, ...]:
        rules = []
        for tool in self.admission.tools:
            risk = RiskLevel(tool.risk_level)
            approval = risk in {RiskLevel.R2, RiskLevel.R3}
            obligations = [
                "audit",
                "read_only",
                "content_hash",
                "evidence_bound",
                "remote_tool_admission",
            ]
            if approval:
                obligations.append("approval")
            rules.append(
                PolicyRule(
                    tool_name=tool.gateway_tool,
                    risk_level=risk,
                    allowed_actions=frozenset({tool.action}),
                    allowed_roles=frozenset(str(role) for role in tool.allowed_roles),
                    requires_approval=approval,
                    obligations=tuple(obligations),
                )
            )
        return tuple(rules)

    def _handler(self, tool: McpToolAdmission):
        async def execute(arguments: dict[str, Any]) -> McpReadEvidence:
            return await self._execute(tool, arguments)

        # ``ToolGateway`` is synchronous. The policy decision, audit and approval
        # consumption happen before this handler runs; returning the coroutine
        # lets an async product entry do network/session work in its own loop so
        # cancellation and timeout can actually close the session.
        def handler(arguments: dict[str, Any]) -> Any:
            return execute(arguments)

        return handler

    def request_for(
        self,
        *,
        actor: ActorContext,
        task_id: UUID,
        remote_tool: str,
        arguments: Mapping[str, Any],
    ) -> ToolRequest:
        """Build the exact gateway request; approval fingerprints use this shape."""

        entry = self._entry(remote_tool)
        payload = dict(arguments)
        validate_mcp_arguments(entry.input_schema, payload)
        return ToolRequest(
            task_id=task_id,
            user_id=actor.user_id,
            tenant_id=actor.tenant_id,
            project_id=actor.project_id,
            agent_role=self.agent_role,
            tool_name=entry.gateway_tool,
            action=entry.action,
            resource=entry.remote_tool,
            arguments={
                "actor_context": actor.model_dump(mode="json"),
                "task_id": str(task_id),
                "remote_tool": entry.remote_tool,
                "arguments": payload,
            },
            data_domains=actor.data_domains,
            requested_risk=RiskLevel(entry.risk_level),
        )

    async def read_async(
        self,
        *,
        actor: ActorContext,
        task_id: UUID,
        remote_tool: str,
        arguments: Mapping[str, Any],
        trace_id: str | None = None,
        approval_id: UUID | None = None,
    ) -> McpReadEvidence:
        if not isinstance(actor, ActorContext):
            raise ValueError("mcp_actor_required")
        if not isinstance(task_id, UUID):
            raise ValueError("mcp_task_id_required")
        entry = self._entry(remote_tool)
        payload = dict(arguments)
        validate_mcp_arguments(entry.input_schema, payload)
        trace = trace_id or uuid4().hex
        if not isinstance(trace, str) or not trace or len(trace) > 128:
            raise ValueError("mcp_trace_id_invalid")
        request = self.request_for(
            actor=actor,
            task_id=task_id,
            remote_tool=entry.remote_tool,
            arguments=payload,
        )
        result = self.gateway.invoke(request, trace_id=trace, approval_id=approval_id)
        if not inspect.isawaitable(result):
            raise RuntimeError("mcp_handler_contract_violation")
        return await result

    def read(
        self,
        *,
        actor: ActorContext,
        task_id: UUID,
        remote_tool: str,
        arguments: Mapping[str, Any],
        trace_id: str | None = None,
        approval_id: UUID | None = None,
    ) -> McpReadEvidence:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.read_async(
                    actor=actor,
                    task_id=task_id,
                    remote_tool=remote_tool,
                    arguments=arguments,
                    trace_id=trace_id,
                    approval_id=approval_id,
                )
            )
        raise RuntimeError("mcp_read_requires_async_context")

    async def aclose(self) -> None:
        await self.sessions.aclose()

    def _entry(self, remote_tool: str) -> McpToolAdmission:
        entry = self._tools.get(remote_tool)
        if entry is None:
            raise McpToolNotAdmitted("mcp_tool_not_admitted")
        return entry

    async def _execute(self, entry: McpToolAdmission, payload: dict[str, Any]) -> McpReadEvidence:
        if set(payload) != {"actor_context", "task_id", "remote_tool", "arguments"}:
            raise ValueError("mcp_internal_payload_invalid")
        actor = ActorContext.model_validate(payload["actor_context"])
        task_id = UUID(payload["task_id"])
        if payload["remote_tool"] != entry.remote_tool:
            raise ValueError("mcp_internal_payload_invalid")
        remote_arguments = payload["arguments"]
        validate_mcp_arguments(entry.input_schema, remote_arguments)

        self._require_admission(actor)
        credential = resolve_credential(self.profile)
        async with self.sessions.lease(
            self.profile,
            credential,
            tenant_id=actor.tenant_id,
            project_id=actor.project_id,
            user_id=actor.user_id,
        ) as session:
            discovery = build_discovery(
                self.profile,
                session.protocol_version,
                await session.list_tools(),
                scanner=self.scanner,
            )
            self._match_admission(discovery)
            self._emit(
                "mcp.schema.verified",
                actor=actor,
                task_id=task_id,
                payload={
                    "server_id": self.profile.server_id,
                    "server_version": self.profile.version,
                    "schema_sha256": discovery.schema_sha256,
                    "tool_count": len(discovery.tools),
                },
            )
            result = await session.call_tool(entry.remote_tool, remote_arguments)
            protocol_version = session.protocol_version

        try:
            content = _canonical(
                {
                    "server_id": self.profile.server_id,
                    "server_version": self.profile.version,
                    "remote_tool": entry.remote_tool,
                    "arguments": remote_arguments,
                    "result": result,
                }
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise McpResponseDenied("mcp_response_not_serializable", str(exc)) from exc
        if len(content) > self.profile.max_response_bytes:
            raise McpResponseTooLarge("mcp_response_too_large")
        try:
            self.scanner.check(content)
        except DLPDeniedError as exc:
            raise McpResponseDenied("mcp_content_denied", str(exc)) from exc

        try:
            artifact = self.store.put_bytes(
                task_id=task_id,
                artifact_type=_ARTIFACT_TYPE,
                content=content,
                created_by=actor.user_id,
                extension="json",
            )
        except DLPDeniedError as exc:
            raise McpResponseDenied("mcp_content_denied", str(exc)) from exc

        evidence = McpReadEvidence(
            task_id=task_id,
            server_id=self.profile.server_id,
            server_version=self.profile.version,
            protocol_version=protocol_version,
            remote_tool=entry.remote_tool,
            gateway_tool=entry.gateway_tool,
            schema_sha256=self.admission.schema_sha256,
            admission_hash=self.admission_hash,
            arguments_hash=_content_hash(remote_arguments),
            response_hash=artifact.content_hash,
            artifact=artifact,
            approval_id=None,
            read_at=self.clock(),
        )
        self._emit(
            "mcp.read.completed",
            actor=actor,
            task_id=task_id,
            payload={
                "server_id": self.profile.server_id,
                "remote_tool": entry.remote_tool,
                "gateway_tool": entry.gateway_tool,
                "schema_sha256": self.admission.schema_sha256,
                "admission_hash": self.admission_hash,
                "artifact_id": str(artifact.artifact_id),
                "artifact_hash": artifact.content_hash,
                "size_bytes": len(content),
            },
        )
        return evidence

    def _require_admission(self, actor: ActorContext) -> None:
        ExtensionAdmission(self.registry).require(
            "connector",
            self.profile.server_id,
            self.profile.version,
            self.admission_hash,
            actor,
        )

    def _match_admission(self, discovery: McpDiscovery) -> None:
        if (
            discovery.server_id != self.profile.server_id
            or discovery.version != self.profile.version
        ):
            raise McpSchemaDrift("mcp_schema_drift")
        expected = {
            tool.remote_tool: (
                tool.gateway_tool,
                tool.description_sha256,
                _canonical(tool.input_schema),
            )
            for tool in self.admission.tools
        }
        observed = {
            tool.name: (
                f"{self.profile.tool_prefix}.{tool.name.replace('-', '_')}",
                tool.description_sha256,
                _canonical(tool.input_schema),
            )
            for tool in discovery.tools
        }
        if set(expected) != set(observed) or expected != observed:
            raise McpSchemaDrift("mcp_schema_drift")
        if discovery.schema_sha256 != self.admission.schema_sha256:
            raise McpSchemaDrift("mcp_schema_drift")

    def _emit(
        self,
        event_type: str,
        *,
        actor: ActorContext,
        task_id: UUID,
        payload: dict[str, Any],
    ) -> None:
        if self.audit is None:
            return
        self.audit.emit(
            AuditEvent(
                event_type=event_type,
                task_id=task_id,
                actor_id=actor.user_id,
                tenant_id=actor.tenant_id,
                trace_id=uuid4().hex,
                payload=payload,
            )
        )


def _content_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()
