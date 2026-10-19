"""One-time approval registry for local development and deterministic tests."""

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Literal, Protocol
from uuid import UUID

from generaldataagent.contracts import (
    ApprovalDisposition,
    ApprovalGrant,
    ApprovalRequest,
    ArtifactClassification,
    ToolRequest,
)
from generaldataagent.contracts.approval import ApprovalRecord


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

    def validate(self, approval_id: UUID, request: ToolRequest) -> ApprovalGrant: ...

    def consume(self, approval_id: UUID, request: ToolRequest) -> ApprovalGrant: ...

    def reject(
        self,
        approval_id: UUID,
        *,
        reviewer_id: str,
        reason: str,
    ) -> ApprovalDisposition: ...

    def cancel(
        self,
        approval_id: UUID,
        *,
        requester_id: str,
        reason: str,
    ) -> ApprovalDisposition: ...


Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


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
        data_domains=request.data_domains,
        impact_scope=impact_scope,
        cost_estimate=cost_estimate,
        classification=classification,
        rollback_plan=rollback_plan,
        expires_at=expires_at,
    )


def _approval_grant(
    request: ApprovalRequest,
    *,
    approver_id: str,
    now: datetime,
) -> ApprovalGrant:
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
    *,
    now: datetime,
) -> None:
    fingerprint = request_fingerprint(request)
    if grant.expires_at <= now:
        raise ApprovalError("approval grant has expired")
    if grant.tenant_id != request.tenant_id:
        raise ApprovalError("approval grant tenant does not match request")
    if approval_request.request_fingerprint != fingerprint:
        raise ApprovalError("approved action does not match final request")
    if grant.request_fingerprint != fingerprint:
        raise ApprovalError("approval grant fingerprint does not match request")


def _disposition(
    request: ApprovalRequest,
    *,
    disposition: Literal["rejected", "cancelled"],
    actor_id: str,
    reason: str,
) -> ApprovalDisposition:
    return ApprovalDisposition(
        approval_id=request.approval_id,
        request_fingerprint=request.request_fingerprint,
        disposition=disposition,
        actor_id=actor_id,
        tenant_id=request.tenant_id,
        reason=reason,
    )


class InMemoryApprovalStore:
    """Stores explicit approvals outside the tool gateway and consumes each once."""

    def __init__(self, *, clock: Clock = _utc_now) -> None:
        self._requests: dict[UUID, ApprovalRequest] = {}
        self._grants: dict[UUID, ApprovalGrant] = {}
        self._consumed: set[UUID] = set()
        self._dispositions: dict[UUID, ApprovalDisposition] = {}
        self._clock = clock
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
            if approval_id in self._dispositions:
                raise ApprovalError("approval request is already closed")
            grant = _approval_grant(
                request,
                approver_id=approver_id,
                now=self._clock(),
            )
            self._grants[approval_id] = grant
            return grant

    def validate(self, approval_id: UUID, request: ToolRequest) -> ApprovalGrant:
        with self._lock:
            if approval_id in self._consumed or approval_id in self._dispositions:
                raise ApprovalError("approval is closed")
            try:
                original = self._requests[approval_id]
                grant = self._grants[approval_id]
            except KeyError as exc:
                raise ApprovalError("approval grant was not found") from exc
            _validate_grant(original, grant, request, now=self._clock())
            return grant

    def consume(self, approval_id: UUID, request: ToolRequest) -> ApprovalGrant:
        with self._lock:
            if approval_id in self._consumed:
                raise ApprovalError("approval grant has already been consumed")
            if approval_id in self._dispositions:
                disposition = self._dispositions[approval_id].disposition
                raise ApprovalError(f"approval request was {disposition}")
            try:
                approval_request = self._requests[approval_id]
                grant = self._grants[approval_id]
            except KeyError as exc:
                raise ApprovalError("approval grant was not found") from exc
            _validate_grant(approval_request, grant, request, now=self._clock())
            self._consumed.add(approval_id)
            return grant

    def reject(
        self,
        approval_id: UUID,
        *,
        reviewer_id: str,
        reason: str,
    ) -> ApprovalDisposition:
        with self._lock:
            try:
                request = self._requests[approval_id]
            except KeyError as exc:
                raise ApprovalError("unknown approval request") from exc
            if approval_id in self._grants or approval_id in self._consumed:
                raise ApprovalError("approved or consumed request cannot be rejected")
            if approval_id in self._dispositions:
                raise ApprovalError("approval request is already closed")
            if reviewer_id == request.user_id:
                raise ApprovalError("approval requester cannot reject their own action")
            result = _disposition(
                request,
                disposition="rejected",
                actor_id=reviewer_id,
                reason=reason,
            )
            self._dispositions[approval_id] = result
            return result

    def cancel(
        self,
        approval_id: UUID,
        *,
        requester_id: str,
        reason: str,
    ) -> ApprovalDisposition:
        with self._lock:
            try:
                request = self._requests[approval_id]
            except KeyError as exc:
                raise ApprovalError("unknown approval request") from exc
            if requester_id != request.user_id:
                raise ApprovalError("only the approval requester can cancel the action")
            if approval_id in self._consumed:
                raise ApprovalError("consumed approval cannot be cancelled")
            if approval_id in self._dispositions:
                raise ApprovalError("approval request is already closed")
            result = _disposition(
                request,
                disposition="cancelled",
                actor_id=requester_id,
                reason=reason,
            )
            self._dispositions[approval_id] = result
            return result


class SQLiteApprovalStore:
    """Durable local approval registry with atomic one-time consumption."""

    def __init__(self, path: Path, *, clock: Clock = _utc_now) -> None:
        if path.exists() and not path.is_file():
            raise ValueError("approval database path must be a file")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._clock = clock
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY,
                    request_json TEXT NOT NULL,
                    grant_json TEXT,
                    consumed_at TEXT,
                    disposition_json TEXT
                )
                """
            )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(approvals)").fetchall()
            }
            if "disposition_json" not in columns:
                connection.execute("ALTER TABLE approvals ADD COLUMN disposition_json TEXT")

    def create_business_request_once(self, request: ToolRequest, **metadata) -> ApprovalRequest:
        """Independent action-ID fence survives loss/restore of business process state."""
        if request.tool_name != "business.draft":
            return self.create_request(request, **metadata)
        approval = _approval_request(request, **metadata)
        action_id = request.arguments.get("action_id")
        logical = (
            str(UUID(str(action_id))) if action_id is not None else request_fingerprint(request)
        )
        key = json.dumps([request.user_id, request.tenant_id, request.project_id, logical])
        sealed = approval.model_dump(mode="json", exclude={"approval_id", "created_at"})
        metadata_hash = hashlib.sha256(json.dumps(sealed, sort_keys=True).encode()).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS business_approval_fences "
                "(logical_key TEXT PRIMARY KEY, metadata_hash TEXT, approval_id TEXT)"
            )
            existing = connection.execute(
                "SELECT metadata_hash,approval_id FROM business_approval_fences "
                "WHERE logical_key=?",
                (hashlib.sha256(key.encode()).hexdigest(),),
            ).fetchone()
            if existing:
                if existing[0] != metadata_hash:
                    raise ApprovalError("business_action_approval_conflict")
                row = connection.execute(
                    "SELECT request_json FROM approvals WHERE approval_id=?", (existing[1],)
                ).fetchone()
                if row is None:
                    raise ApprovalError("business_approval_fence_corrupt")
                prior = ApprovalRequest.model_validate_json(row[0])
                check = prior.model_dump(mode="json", exclude={"approval_id", "created_at"})
                if check != sealed:
                    raise ApprovalError("business_approval_fence_corrupt")
                return prior
            connection.execute(
                "INSERT INTO approvals(approval_id,request_json) VALUES(?,?)",
                (str(approval.approval_id), approval.model_dump_json()),
            )
            connection.execute(
                "INSERT INTO business_approval_fences VALUES(?,?,?)",
                (
                    hashlib.sha256(key.encode()).hexdigest(),
                    metadata_hash,
                    str(approval.approval_id),
                ),
            )
        return approval

    def inspect(self, approval_id: UUID) -> ApprovalRecord:
        """Trusted service port; the caller must enforce session/scope before disclosure."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT request_json, grant_json, consumed_at, disposition_json "
                "FROM approvals WHERE approval_id=?",
                (str(approval_id),),
            ).fetchone()
        if row is None:
            raise ApprovalError("unknown approval request")
        return ApprovalRecord(
            request=ApprovalRequest.model_validate_json(row[0]),
            grant=ApprovalGrant.model_validate_json(row[1]) if row[1] else None,
            consumed=row[2] is not None,
            disposition=ApprovalDisposition.model_validate_json(row[3]) if row[3] else None,
        )

    def review_page(
        self,
        *,
        tenant: str,
        project: str,
        domains: frozenset[str],
        before: int | None = None,
        limit: int = 25,
    ) -> dict:
        if (
            type(limit) is not int
            or not 1 <= limit <= 100
            or (before is not None and (type(before) is not int or before <= 0))
        ):
            raise ValueError("invalid_approval_page")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT rowid,approval_id FROM approvals "
                "WHERE json_extract(request_json,'$.tenant_id')=? "
                "AND json_extract(request_json,'$.project_id')=? "
                "AND json_array_length(json_extract(request_json,'$.data_domains'))>0 "
                "AND NOT EXISTS (SELECT 1 FROM json_each(request_json,'$.data_domains') d "
                "WHERE d.value NOT IN (SELECT value FROM json_each(?))) "
                "AND (? IS NULL OR rowid<?) ORDER BY rowid DESC LIMIT ?",
                (tenant, project, json.dumps(sorted(domains)), before, before, limit + 1),
            ).fetchall()
        return {
            "items": [self.inspect(UUID(row[1])) for row in rows[:limit]],
            "next_cursor": rows[limit - 1][0] if len(rows) > limit else None,
        }

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
                "SELECT request_json, grant_json, disposition_json "
                "FROM approvals WHERE approval_id = ?",
                (str(approval_id),),
            ).fetchone()
            if row is None:
                raise ApprovalError("unknown approval request")
            if row[1] is not None:
                raise ApprovalError("approval request already has a grant")
            if row[2] is not None:
                raise ApprovalError("approval request is already closed")
            request = ApprovalRequest.model_validate_json(row[0])
            grant = _approval_grant(
                request,
                approver_id=approver_id,
                now=self._clock(),
            )
            updated = connection.execute(
                "UPDATE approvals SET grant_json = ? "
                "WHERE approval_id = ? AND grant_json IS NULL AND disposition_json IS NULL",
                (grant.model_dump_json(), str(approval_id)),
            )
            if updated.rowcount != 1:
                raise ApprovalError("approval request already has a grant")
        return grant

    def validate(self, approval_id: UUID, request: ToolRequest) -> ApprovalGrant:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT request_json, grant_json, consumed_at, disposition_json "
                "FROM approvals WHERE approval_id = ?",
                (str(approval_id),),
            ).fetchone()
            if row is None or row[1] is None:
                raise ApprovalError("approval grant was not found")
            if row[2] is not None or row[3] is not None:
                raise ApprovalError("approval is closed")
            grant = ApprovalGrant.model_validate_json(row[1])
            _validate_grant(
                ApprovalRequest.model_validate_json(row[0]), grant, request, now=self._clock()
            )
            return grant

    def consume(self, approval_id: UUID, request: ToolRequest) -> ApprovalGrant:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT request_json, grant_json, consumed_at, disposition_json "
                "FROM approvals WHERE approval_id = ?",
                (str(approval_id),),
            ).fetchone()
            if row is None:
                raise ApprovalError("approval grant was not found")
            if row[2] is not None:
                raise ApprovalError("approval grant has already been consumed")
            if row[3] is not None:
                disposition = ApprovalDisposition.model_validate_json(row[3]).disposition
                raise ApprovalError(f"approval request was {disposition}")
            if row[1] is None:
                raise ApprovalError("approval grant was not found")
            approval_request = ApprovalRequest.model_validate_json(row[0])
            grant = ApprovalGrant.model_validate_json(row[1])
            _validate_grant(
                approval_request,
                grant,
                request,
                now=self._clock(),
            )
            consumed_at = self._clock().isoformat()
            updated = connection.execute(
                "UPDATE approvals SET consumed_at = ? "
                "WHERE approval_id = ? AND consumed_at IS NULL AND disposition_json IS NULL",
                (consumed_at, str(approval_id)),
            )
            if updated.rowcount != 1:
                raise ApprovalError("approval grant has already been consumed")
        return grant

    def reject(
        self,
        approval_id: UUID,
        *,
        reviewer_id: str,
        reason: str,
    ) -> ApprovalDisposition:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT request_json, grant_json, consumed_at, disposition_json "
                "FROM approvals WHERE approval_id = ?",
                (str(approval_id),),
            ).fetchone()
            if row is None:
                raise ApprovalError("unknown approval request")
            if row[1] is not None or row[2] is not None:
                raise ApprovalError("approved or consumed request cannot be rejected")
            if row[3] is not None:
                raise ApprovalError("approval request is already closed")
            request = ApprovalRequest.model_validate_json(row[0])
            if reviewer_id == request.user_id:
                raise ApprovalError("approval requester cannot reject their own action")
            result = _disposition(
                request,
                disposition="rejected",
                actor_id=reviewer_id,
                reason=reason,
            )
            updated = connection.execute(
                "UPDATE approvals SET disposition_json = ? "
                "WHERE approval_id = ? AND grant_json IS NULL "
                "AND consumed_at IS NULL AND disposition_json IS NULL",
                (result.model_dump_json(), str(approval_id)),
            )
            if updated.rowcount != 1:
                raise ApprovalError("approval request is already closed")
        return result

    def cancel(
        self,
        approval_id: UUID,
        *,
        requester_id: str,
        reason: str,
    ) -> ApprovalDisposition:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT request_json, consumed_at, disposition_json "
                "FROM approvals WHERE approval_id = ?",
                (str(approval_id),),
            ).fetchone()
            if row is None:
                raise ApprovalError("unknown approval request")
            request = ApprovalRequest.model_validate_json(row[0])
            if requester_id != request.user_id:
                raise ApprovalError("only the approval requester can cancel the action")
            if row[1] is not None:
                raise ApprovalError("consumed approval cannot be cancelled")
            if row[2] is not None:
                raise ApprovalError("approval request is already closed")
            result = _disposition(
                request,
                disposition="cancelled",
                actor_id=requester_id,
                reason=reason,
            )
            updated = connection.execute(
                "UPDATE approvals SET disposition_json = ? "
                "WHERE approval_id = ? AND consumed_at IS NULL AND disposition_json IS NULL",
                (result.model_dump_json(), str(approval_id)),
            )
            if updated.rowcount != 1:
                raise ApprovalError("approval request is already closed")
        return result

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5.0)
