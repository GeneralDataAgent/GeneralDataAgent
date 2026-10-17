"""Restart-safe approval pauses bound to original thread and current identity."""

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import UUID

from generaldataagent.contracts import ApprovalRequest, ToolRequest
from generaldataagent.policy.approvals import ApprovalError, request_fingerprint
from generaldataagent.security.identity import SessionBroker
from generaldataagent.tools.gateway import ToolGateway


class PendingActionCoordinator:
    """Trusted runtime port. Agent/API inputs cannot replace persisted action arguments.

    A claimed action is never automatically replayed after failure or a process crash.
    Operators must reconcile an executing/failed action before creating another approval.
    """

    def __init__(self, path: Path, *, broker: SessionBroker, gateway: ToolGateway) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self.broker, self.gateway = path, broker, gateway
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS pending_actions ("
                "thread_id TEXT PRIMARY KEY, request_json TEXT NOT NULL, "
                "approval_id TEXT NOT NULL UNIQUE, state TEXT NOT NULL)"
            )

    def pause(self, thread_id: str, request: ToolRequest, approval: ApprovalRequest) -> None:
        if not thread_id.strip() or len(thread_id) > 128:
            raise ValueError("invalid thread id")
        if self.gateway.policy.evaluate(request).decision != "require_approval":
            raise ApprovalError("only approval-gated actions can be paused")
        if approval.request_fingerprint != request_fingerprint(request):
            raise ApprovalError("approval does not match paused action")
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            try:
                db.execute(
                    "INSERT INTO pending_actions VALUES (?, ?, ?, 'pending')",
                    (thread_id, request.model_dump_json(), str(approval.approval_id)),
                )
            except sqlite3.IntegrityError as exc:
                raise ApprovalError("thread or approval already registered") from exc

    def resume(self, thread_id: str, token: str) -> Any:
        identity = self.broker.authenticate(token)
        if "actions:resume" not in identity.permissions:
            raise ApprovalError("action_access_denied")
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT request_json, approval_id, state FROM pending_actions WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            if row is None:
                raise ApprovalError("action_access_denied")
            request = ToolRequest.model_validate_json(row[0])
            if (
                (request.user_id, request.tenant_id, request.project_id)
                != (identity.user_id, identity.tenant_id, identity.project_id)
                or not request.data_domains
                or not set(request.data_domains) <= identity.data_domains
            ):
                raise ApprovalError("action_access_denied")
            if row[2] != "pending":
                raise ApprovalError("action_already_claimed")
            # Recheck current policy before claiming. Gateway repeats it before execution.
            if self.gateway.policy.evaluate(request).decision != "require_approval":
                raise ApprovalError("action_policy_changed")
            if self.gateway.approvals is None:
                raise ApprovalError("approval service unavailable")
            self.broker.authenticate(token)
            self.gateway.approvals.validate(UUID(row[1]), request)
            db.execute(
                "UPDATE pending_actions SET state = 'executing' WHERE thread_id = ?", (thread_id,)
            )
        try:
            result = self.gateway.invoke(request, trace_id=thread_id, approval_id=UUID(row[1]))
        except Exception:
            self._finish(thread_id, "failed")
            raise
        self._finish(thread_id, "completed")
        return result

    def _finish(self, thread_id: str, state: str) -> None:
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute(
                "UPDATE pending_actions SET state = ? WHERE thread_id = ?", (state, thread_id)
            )
