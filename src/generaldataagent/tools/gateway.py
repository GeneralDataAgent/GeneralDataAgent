"""Tool gateway that makes policy evaluation unavoidable."""

from collections.abc import Callable
from contextlib import nullcontext, suppress
from typing import Any
from uuid import UUID

from generaldataagent.contracts import ActorContext, AuditEvent, ToolRequest
from generaldataagent.observability import JsonlAuditSink
from generaldataagent.observability.alerts import AlertDeliveryError, CriticalAlertRouter
from generaldataagent.observability.verified_audit import AuditUnavailableError, VerifiedAuditSink
from generaldataagent.policy import ApprovalStore, PolicyEngine, request_fingerprint
from generaldataagent.policy.budgets import TaskBudgetLedger


class PolicyDeniedError(PermissionError):
    pass


class ToolGateway:
    def __init__(
        self,
        policy: PolicyEngine,
        audit: JsonlAuditSink | VerifiedAuditSink | None = None,
        approvals: ApprovalStore | None = None,
        *,
        production_mode: bool = False,
        alerts: CriticalAlertRouter | None = None,
        budgets: TaskBudgetLedger | None = None,
        request_scope: Callable | None = None,
    ) -> None:
        if production_mode and not isinstance(audit, VerifiedAuditSink):
            raise ValueError("production gateway requires independent verified audit")
        self.budgets = budgets
        self.request_scope = request_scope
        self.alerts = alerts
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
        scope = self.request_scope(request) if self.request_scope is not None else nullcontext()
        with scope:
            return self._invoke_scoped(request, trace_id=trace_id, approval_id=approval_id)

    def _invoke_scoped(
        self, request: ToolRequest, *, trace_id: str, approval_id: UUID | None
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
            if self.budgets is not None:
                self.approvals.validate(approval_id, request)
                self._reserve(request)
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
        if decision.decision == "allow" and self.budgets is not None:
            self._reserve(request)
        return handler(request.arguments)

    def _reserve(self, request: ToolRequest) -> None:
        if self.budgets is not None:
            actor = ActorContext(
                user_id=request.user_id,
                tenant_id=request.tenant_id,
                project_id=request.project_id,
                data_domains=request.data_domains,
            )
            self.budgets.reserve(request.task_id, actor)

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
        try:
            self.audit.emit(
                AuditEvent(
                    event_type=event_type,
                    task_id=request.task_id,
                    actor_id=request.user_id,
                    tenant_id=request.tenant_id,
                    trace_id=trace_id,
                    payload={**payload, "request_fingerprint": request_fingerprint(request)},
                )
            )
        except AuditUnavailableError:
            if self.alerts is not None:
                # Pending intent survives a delivery outage for independent retry.
                with suppress(AlertDeliveryError):
                    self.alerts.signal("audit_outage")
            raise
