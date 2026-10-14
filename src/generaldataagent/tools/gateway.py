"""Tool gateway that makes policy evaluation unavoidable."""

from collections.abc import Callable
from typing import Any
from uuid import UUID

from generaldataagent.contracts import AuditEvent, ToolRequest
from generaldataagent.observability import JsonlAuditSink
from generaldataagent.policy import InMemoryApprovalStore, PolicyEngine


class PolicyDeniedError(PermissionError):
    pass


class ToolGateway:
    def __init__(
        self,
        policy: PolicyEngine,
        audit: JsonlAuditSink | None = None,
        approvals: InMemoryApprovalStore | None = None,
    ) -> None:
        self.policy = policy
        self.audit = audit
        self.approvals = approvals
        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = {}

    def register(self, tool_name: str, handler: Callable[[dict[str, Any]], Any]) -> None:
        if tool_name in self._handlers:
            raise ValueError(f"tool {tool_name!r} is already registered")
        self._handlers[tool_name] = handler

    def invoke(
        self,
        request: ToolRequest,
        *,
        trace_id: str,
        approval_id: UUID | None = None,
    ) -> Any:
        decision = self.policy.evaluate(request)
        self._audit(request, decision.model_dump(mode="json"), trace_id)
        if decision.decision == "require_approval":
            handler = self._handlers.get(request.tool_name)
            if handler is None:
                raise LookupError(f"no handler registered for {request.tool_name!r}")
            if self.approvals is None or approval_id is None:
                raise PolicyDeniedError(
                    f"tool request was not allowed: {decision.decision}/{decision.reason_code}"
                )
            grant = self.approvals.consume(approval_id, request)
            self._audit(
                request,
                {
                    "decision": "approved",
                    "approval_id": str(grant.approval_id),
                    "approver_id": grant.approver_id,
                    "request_fingerprint": grant.request_fingerprint,
                },
                trace_id,
                event_type="approval.consumed",
            )
        elif decision.decision != "allow":
            raise PolicyDeniedError(
                f"tool request was not allowed: {decision.decision}/{decision.reason_code}"
            )
        else:
            handler = self._handlers.get(request.tool_name)
            if handler is None:
                raise LookupError(f"no handler registered for {request.tool_name!r}")
        return handler(request.arguments)

    def _audit(
        self,
        request: ToolRequest,
        payload: dict[str, Any],
        trace_id: str,
        *,
        event_type: str = "policy.decision",
    ) -> None:
        if self.audit is None:
            return
        self.audit.emit(
            AuditEvent(
                event_type=event_type,
                task_id=request.task_id,
                actor_id=request.user_id,
                tenant_id=request.tenant_id,
                trace_id=trace_id,
                payload=payload,
            )
        )
