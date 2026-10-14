"""One-time approval registry for local development and deterministic tests."""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Protocol
from uuid import UUID

from generaldataagent.contracts import (
    ApprovalGrant,
    ApprovalRequest,
    ArtifactClassification,
    ToolRequest,
)


class ApprovalError(PermissionError):
    pass


class ApprovalStore(Protocol):
    def create_request(
        self,
        request: ToolRequest,
        *,
        impact_scope: str,
        cost_estimate: str,
        classification: ArtifactClassification,
        rollback_plan: str,
        expires_at: datetime,
    ) -> ApprovalRequest: ...

    def approve(self, approval_id: UUID, *, approver_id: str) -> ApprovalGrant: ...

    def consume(self, approval_id: UUID, request: ToolRequest) -> ApprovalGrant: ...


def request_fingerprint(request: ToolRequest) -> str:
    payload = request.model_dump(mode="json", exclude={"request_id"})
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _approval_request(
    request: ToolRequest,
    *,
    impact_scope: str,
    cost_estimate: str,
    classification: ArtifactClassification,
    rollback_plan: str,
    expires_at: datetime,
) -> ApprovalRequest:
    return ApprovalRequest(
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


def _approval_grant(request: ApprovalRequest, *, approver_id: str) -> ApprovalGrant:
    now = datetime.now(UTC)
    if request.expires_at <= now:
        raise ApprovalError("approval request has expired")
    if approver_id == request.user_id:
        raise ApprovalError("approval requester cannot approve their own action")
    return ApprovalGrant(
        approval_id=request.approval_id,
        request_fingerprint=request.request_fingerprint,
        approver_id=approver_id,
        tenant_id=request.tenant_id,
        expires_at=request.expires_at,
    )


def _validate_grant(
    approval_request: ApprovalRequest,
    grant: ApprovalGrant,
    request: ToolRequest,
) -> None:
    fingerprint = request_fingerprint(request)
    if grant.expires_at <= datetime.now(UTC):
        raise ApprovalError("approval grant has expired")
    if grant.tenant_id != request.tenant_id:
        raise ApprovalError("approval grant tenant does not match request")
    if approval_request.request_fingerprint != fingerprint:
        raise ApprovalError("approved action does not match final request")
    if grant.request_fingerprint != fingerprint:
        raise ApprovalError("approval grant fingerprint does not match request")


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
        approval = _approval_request(
            request,
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
        with self._lock:
            try:
                request = self._requests[approval_id]
            except KeyError as exc:
                raise ApprovalError("unknown approval request") from exc
            if approval_id in self._grants:
                raise ApprovalError("approval request already has a grant")
            grant = _approval_grant(request, approver_id=approver_id)
            self._grants[approval_id] = grant
            return grant

    def consume(self, approval_id: UUID, request: ToolRequest) -> ApprovalGrant:
        with self._lock:
            if approval_id in self._consumed:
                raise ApprovalError("approval grant has already been consumed")
            try:
                approval_request = self._requests[approval_id]
                grant = self._grants[approval_id]
            except KeyError as exc:
                raise ApprovalError("approval grant was not found") from exc
            _validate_grant(approval_request, grant, request)
            self._consumed.add(approval_id)
            return grant


class SQLiteApprovalStore:
    """Durable local approval registry with atomic one-time consumption."""

    def __init__(self, path: Path) -> None:
        if path.exists() and not path.is_file():
            raise ValueError("approval database path must be a file")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY,
                    request_json TEXT NOT NULL,
                    grant_json TEXT,
                    consumed_at TEXT
                )
                """
            )

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
        approval = _approval_request(
            request,
            impact_scope=impact_scope,
            cost_estimate=cost_estimate,
            classification=classification,
            rollback_plan=rollback_plan,
            expires_at=expires_at,
        )
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO approvals (approval_id, request_json) VALUES (?, ?)",
                (str(approval.approval_id), approval.model_dump_json()),
            )
        return approval

    def approve(self, approval_id: UUID, *, approver_id: str) -> ApprovalGrant:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT request_json, grant_json FROM approvals WHERE approval_id = ?",
                (str(approval_id),),
            ).fetchone()
            if row is None:
                raise ApprovalError("unknown approval request")
            if row[1] is not None:
                raise ApprovalError("approval request already has a grant")
            request = ApprovalRequest.model_validate_json(row[0])
            grant = _approval_grant(request, approver_id=approver_id)
            updated = connection.execute(
                "UPDATE approvals SET grant_json = ? "
                "WHERE approval_id = ? AND grant_json IS NULL",
                (grant.model_dump_json(), str(approval_id)),
            )
            if updated.rowcount != 1:
                raise ApprovalError("approval request already has a grant")
        return grant

    def consume(self, approval_id: UUID, request: ToolRequest) -> ApprovalGrant:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT request_json, grant_json, consumed_at "
                "FROM approvals WHERE approval_id = ?",
                (str(approval_id),),
            ).fetchone()
            if row is None or row[1] is None:
                raise ApprovalError("approval grant was not found")
            if row[2] is not None:
                raise ApprovalError("approval grant has already been consumed")
            approval_request = ApprovalRequest.model_validate_json(row[0])
            grant = ApprovalGrant.model_validate_json(row[1])
            _validate_grant(approval_request, grant, request)
            consumed_at = datetime.now(UTC).isoformat()
            updated = connection.execute(
                "UPDATE approvals SET consumed_at = ? "
                "WHERE approval_id = ? AND consumed_at IS NULL",
                (consumed_at, str(approval_id)),
            )
            if updated.rowcount != 1:
                raise ApprovalError("approval grant has already been consumed")
        return grant

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5.0)
