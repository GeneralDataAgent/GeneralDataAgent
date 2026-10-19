"""Durable report polling with fresh, narrowly scoped service sessions.

The trusted deployment injects a token issuer backed by its workload identity provider.
This module never stores credentials, approves actions, or delivers external messages.
"""

import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field

from generaldataagent.business.actions import content_hash
from generaldataagent.business.schedules import ReportScheduler
from generaldataagent.contracts.business import ActionIntent, ScheduledReport
from generaldataagent.contracts.models import ContractModel
from generaldataagent.security.identity import SessionIdentity


class ScheduleWorkload(ContractModel):
    schedule_id: UUID
    user_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    data_domains: tuple[str, ...] = Field(min_length=1, max_length=32)
    schedule_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    expires_at: AwareDatetime


class ReportWorker:
    """Operator-owned poller; token_provider must issue a NEW token per invocation.

    The provider must authenticate this deployment and recheck its own revocable grant
    for the exact workload. It must not return a stored user session or refresh token.
    Sessions are revoked after each tick. Local job cancellation also cancels the schedule,
    allowing an in-flight refresh to observe cancellation before proposing its draft.
    """

    def __init__(
        self,
        path: Path,
        *,
        scheduler: ReportScheduler,
        token_provider: Callable[[ScheduleWorkload], str],
        poll_seconds: int = 30,
    ):
        if type(poll_seconds) is not int or not 1 <= poll_seconds <= 300:
            raise ValueError("invalid_poll_interval")
        if path.resolve() in {scheduler.path.resolve(), scheduler.actions.path.resolve()}:
            raise ValueError("worker requires a separate protected database")
        self.path, self.scheduler = path, scheduler
        self.token_provider, self.poll_seconds = token_provider, poll_seconds
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS report_jobs ("
                "id TEXT PRIMARY KEY, workload TEXT NOT NULL, enabled INTEGER NOT NULL, "
                "next_poll REAL NOT NULL, lease TEXT, lease_until REAL NOT NULL)"
            )

    def _db(self):
        return closing(sqlite3.connect(self.path, timeout=5))

    def register(self, schedule_id: UUID, token: str, *, expires_at: datetime) -> None:
        identity = self.scheduler.actions._identity(token, "business:propose")
        now = self.scheduler.actions._now()
        if expires_at.tzinfo is None or not now < expires_at <= now + timedelta(days=30):
            raise ValueError("worker_grant_requires_expiry_within_30_days")
        with closing(sqlite3.connect(self.scheduler.path)) as db:
            row, spec = self.scheduler._row(db, schedule_id, identity)
        if not row[5]:
            raise PermissionError("schedule_disabled")
        workload = ScheduleWorkload(
            schedule_id=schedule_id,
            user_id=identity.user_id,
            tenant_id=identity.tenant_id,
            project_id=identity.project_id,
            data_domains=tuple(
                sorted(
                    self.scheduler.actions._control(
                        self.scheduler._intent(spec, 0), identity
                    ).data_domains
                )
            ),
            schedule_hash=content_hash(spec.model_dump(mode="json")),
            expires_at=expires_at,
        )
        with self._db() as db, db:
            db.execute(
                "INSERT INTO report_jobs VALUES(?,?,1,?,NULL,0)",
                (str(schedule_id), workload.model_dump_json(), now.timestamp()),
            )

    def cancel(self, schedule_id: UUID, token: str) -> None:
        # Scheduler verifies ownership and invalidates in-flight generation first.
        self.scheduler.cancel(schedule_id, token)
        with self._db() as db, db:
            db.execute("UPDATE report_jobs SET enabled=0 WHERE id=?", (str(schedule_id),))

    def _authorized_spec(self, workload: ScheduleWorkload):
        if workload.expires_at <= self.scheduler.actions._now():
            raise PermissionError("workload_expired")
        with self._db() as db:
            row = db.execute(
                "SELECT workload,enabled FROM report_jobs WHERE id=?", (str(workload.schedule_id),)
            ).fetchone()
        if row is None or row[1] != 1 or ScheduleWorkload.model_validate_json(row[0]) != workload:
            raise PermissionError("workload_not_registered_or_disabled")
        identity = SessionIdentity(
            workload.user_id,
            workload.tenant_id,
            workload.project_id,
            frozenset(workload.data_domains),
            workload.expires_at,
            frozenset({"business:propose"}),
        )
        with closing(sqlite3.connect(self.scheduler.path)) as db:
            row, spec = self.scheduler._row(db, workload.schedule_id, identity)
        control = self.scheduler.actions._control(self.scheduler._intent(spec, 0), identity)
        if not row[5] or content_hash(spec.model_dump(mode="json")) != workload.schedule_hash:
            raise PermissionError("workload_schedule_changed_or_disabled")
        if control.data_domains != frozenset(workload.data_domains):
            raise PermissionError("workload_control_scope_changed")
        return spec

    def authorizes_workload(self, workload: ScheduleWorkload) -> bool:
        """Trusted identity-issuer port; also require an enrolled service principal."""
        try:
            self._authorized_spec(workload)
            return True
        except (ValueError, PermissionError):
            return False

    def authorizes_action(
        self, workload: ScheduleWorkload, intent: object, content: bytes | None, final: bool
    ) -> bool:
        """Constrain delegated sessions to exact report occurrences of their enrolled job."""
        try:
            spec = self._authorized_spec(workload)
            if not isinstance(intent, ActionIntent) or intent.payload.kind != "report":
                return False
            prefix = f"schedule:{spec.schedule_id}:"
            if not intent.thread_id.startswith(prefix):
                return False
            index = int(intent.thread_id[len(prefix) :])
            if not 0 <= index < spec.occurrences:
                return False
            expected = self.scheduler._intent(spec, index)
            fields = (
                "action_id",
                "task_id",
                "thread_id",
                "idempotency_key",
                "destination",
                "operation",
                "compensates",
                "created_at",
                "expires_at",
            )
            if any(getattr(intent, field) != getattr(expected, field) for field in fields):
                return False
            if (
                spec.revision_policy == "sequential"
                and intent.expected_revision != expected.expected_revision
            ):
                return False
            if (intent.payload.title, intent.payload.period) != (
                expected.payload.title,
                expected.payload.period,
            ):
                return False
            if not final:
                return True
            if spec.query is None:
                return intent.payload.model_copy(
                    update={"publication_text": None}
                ) == expected.payload.model_copy(update={"publication_text": None})
            if content is None or intent.payload.report_artifact is None:
                return False
            frozen = ScheduledReport.model_validate_json(content)
            query = spec.query
            if spec.lookback_minutes is not None:
                query = query.model_copy(
                    update={
                        "start_time": expected.created_at
                        - timedelta(minutes=spec.lookback_minutes),
                        "end_time": expected.created_at,
                    }
                )
            return (
                frozen.schedule_id == spec.schedule_id
                and frozen.index == index
                and frozen.query == query
            )
        except (ValueError, PermissionError):
            return False

    def _check(self, workload: ScheduleWorkload, token: str) -> None:
        actions = self.scheduler.actions
        identity = actions._identity(token, "business:propose")
        if (
            workload.expires_at <= actions._now()
            or identity.permissions != frozenset({"business:propose"})
            or (identity.user_id, identity.tenant_id, identity.project_id)
            != (workload.user_id, workload.tenant_id, workload.project_id)
            or identity.data_domains != frozenset(workload.data_domains)
            or identity.expires_at > workload.expires_at
        ):
            raise PermissionError("worker_identity_mismatch_or_expired")
        with closing(sqlite3.connect(self.scheduler.path)) as db:
            row, spec = self.scheduler._row(db, workload.schedule_id, identity)
        if not row[5] or content_hash(spec.model_dump(mode="json")) != workload.schedule_hash:
            raise PermissionError("worker_schedule_changed_or_disabled")

    def run_once(self, *, limit: int = 10) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid_worker_batch_limit")
        results = []
        for _ in range(limit):
            now = self.scheduler.actions._now().timestamp()
            lease = str(uuid4())
            with self._db() as db, db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT id,workload FROM report_jobs WHERE enabled=1 AND next_poll<=? "
                    "AND lease_until<=? ORDER BY next_poll,id LIMIT 1",
                    (now, now),
                ).fetchone()
                if row is None:
                    break
                db.execute(
                    "UPDATE report_jobs SET lease=?,lease_until=?,next_poll=? WHERE id=?",
                    (lease, now + 900, now + self.poll_seconds, row[0]),
                )
            token = None
            complete = False
            try:
                workload = ScheduleWorkload.model_validate_json(row[1])
                if workload.expires_at <= self.scheduler.actions._now():
                    complete = True
                    raise PermissionError("worker_grant_expired")
                token = self.token_provider(workload)
                self._check(workload, token)
                result = self.scheduler.tick(workload.schedule_id, token, limit=1)
                complete = result["complete"]
                results.append(
                    {"schedule_id": row[0], "state": "complete" if complete else "active"}
                )
            except Exception as exc:
                # Provider and downstream exception messages may contain credentials or query data.
                results.append(
                    {"schedule_id": row[0], "state": "failed", "error": type(exc).__name__}
                )
            finally:
                if token is not None:
                    self.scheduler.actions.broker.revoke(token)
                with self._db() as db, db:
                    db.execute(
                        "UPDATE report_jobs SET lease=NULL,lease_until=0,enabled=? "
                        "WHERE id=? AND lease=? AND enabled=1",
                        (int(not complete), row[0], lease),
                    )
        return results

    def run(self, stop: Event, *, on_result: Callable[[list[dict]], None]) -> None:
        """Run until graceful shutdown; each batch is bounded and emits sanitized status."""
        while not stop.is_set():
            on_result(self.run_once())
            stop.wait(self.poll_seconds)
