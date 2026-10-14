"""One-time approval registry for local development and deterministic tests."""

import hashlib
import json
from datetime import UTC, datetime
from threading import Lock
from uuid import UUID

from generaldataagent.contracts import (
    ApprovalGrant,
    ApprovalRequest,
    ArtifactClassification,
    ToolRequest,
)


class ApprovalError(PermissionError):
    pass


def request_fingerprint(request: ToolRequest) -> str:
    payload = request.model_dump(mode="json", exclude={"request_id"})
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


class InMemoryApprovalStore:
    """Stores explicit approvals outside the tool gateway and consumes each once."""

    def __init__(self) -> None:
        self._requests: dict[UUID, ApprovalRequest] = {}
        self._grants: dict[UUID, ApprovalGrant] = {}
        self._consumed: set[UUID] = set()
        self._lock = Lock()

    def create_request(
        self,
        request: ToolRequest,
        *,
        impact_scope: str,
        cost_estimate: str,
        classification: ArtifactClassification,
        rollback_plan: str,
        expires_at: datetime,
    ) -> ApprovalRequest:
        approval = ApprovalRequest(
            request_fingerprint=request_fingerprint(request),
            task_id=request.task_id,
            user_id=request.user_id,
            tenant_id=request.tenant_id,
            project_id=request.project_id,
            tool_name=request.tool_name,
            action=request.action,
            resource=request.resource,
            parameters=request.arguments,
            impact_scope=impact_scope,
            cost_estimate=cost_estimate,
            classification=classification,
            rollback_plan=rollback_plan,
            expires_at=expires_at,
        )
        with self._lock:
            self._requests[approval.approval_id] = approval
        return approval

    def approve(self, approval_id: UUID, *, approver_id: str) -> ApprovalGrant:
        now = datetime.now(UTC)
        with self._lock:
            try:
                request = self._requests[approval_id]
            except KeyError as exc:
                raise ApprovalError("unknown approval request") from exc
            if request.expires_at <= now:
                raise ApprovalError("approval request has expired")
            if approval_id in self._grants:
                raise ApprovalError("approval request already has a grant")
            if approver_id == request.user_id:
                raise ApprovalError("approval requester cannot approve their own action")
            grant = ApprovalGrant(
                approval_id=approval_id,
                request_fingerprint=request.request_fingerprint,
                approver_id=approver_id,
                tenant_id=request.tenant_id,
                expires_at=request.expires_at,
            )
            self._grants[approval_id] = grant
            return grant

    def consume(self, approval_id: UUID, request: ToolRequest) -> ApprovalGrant:
        now = datetime.now(UTC)
        fingerprint = request_fingerprint(request)
        with self._lock:
            if approval_id in self._consumed:
                raise ApprovalError("approval grant has already been consumed")
            try:
                approval_request = self._requests[approval_id]
                grant = self._grants[approval_id]
            except KeyError as exc:
                raise ApprovalError("approval grant was not found") from exc
            if grant.expires_at <= now:
                raise ApprovalError("approval grant has expired")
            if grant.tenant_id != request.tenant_id:
                raise ApprovalError("approval grant tenant does not match request")
            if approval_request.request_fingerprint != fingerprint:
                raise ApprovalError("approved action does not match final request")
            if grant.request_fingerprint != fingerprint:
                raise ApprovalError("approval grant fingerprint does not match request")
            self._consumed.add(approval_id)
            return grant
