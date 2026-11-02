"""Encrypted local metric-task acceptance and fenced, restartable worker claims."""

import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from typing import Literal
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import AwareDatetime, Field, model_validator

from generaldataagent.contracts import ActorContext, MetricQuery
from generaldataagent.contracts.models import ContractModel

JobStatus = Literal[
    "queued",
    "running",
    "interrupted",
    "completed",
    "failed",
    "needs_human",
    "cancel_requested",
    "cancelled",
]
FailureCode = Literal[
    "authorization_unavailable",
    "execution_interrupted",
    "budget_exhausted",
    "invalid_task",
    "attempts_exhausted",
]


class TaskQueueConflict(ValueError):
    pass


class TaskLeaseLost(RuntimeError):
    pass


class TaskCancellationRequested(RuntimeError):
    pass


class MetricTaskRecord(ContractModel):
    schema_version: Literal["2.0"] = "2.0"
    task_id: UUID
    thread_id: UUID
    status: JobStatus
    attempts: int = Field(ge=0, le=10)
    created_at: AwareDatetime
    expires_at: AwareDatetime
    failure_code: FailureCode | None = None
    cancel_requested_at: AwareDatetime | None = None
    cancelled_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def cancellation_state(self):
        requested = self.status in {"cancel_requested", "cancelled"}
        if requested != (self.cancel_requested_at is not None):
            raise ValueError("task_cancellation_request_binding_invalid")
        if (self.status == "cancelled") != (self.cancelled_at is not None):
            raise ValueError("task_cancellation_stop_binding_invalid")
        if self.cancelled_at is not None and self.cancelled_at < self.cancel_requested_at:
            raise ValueError("task_cancellation_stop_precedes_request")
        return self


class MetricTaskPage(ContractModel):
    schema_version: Literal["2.0"] = "2.0"
    tasks: tuple[MetricTaskRecord, ...]
    next_cursor: str | None = None


class _ListCursor(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    scope: str = Field(pattern=r"^[0-9a-f]{64}$")
    after: int = Field(ge=0, le=2**63 - 1, strict=True)
    upper: int = Field(ge=1, le=2**63 - 1, strict=True)
    anchor: UUID
    expires_at: AwareDatetime


class _Payload(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    task_id: UUID
    thread_id: UUID
    actor: ActorContext
    query: MetricQuery
    session_reference: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: AwareDatetime
    expires_at: AwareDatetime


@dataclass(frozen=True)
class MetricTaskContext:
    record: MetricTaskRecord
    actor: ActorContext
    query: MetricQuery


@dataclass(frozen=True)
class MetricTaskClaim(MetricTaskContext):
    session_reference: str
    lease_token: str


def canonical_actor(actor: ActorContext) -> ActorContext:
    return ActorContext.model_validate(
        actor.model_dump() | {"data_domains": tuple(sorted(set(actor.data_domains)))}
    )


class MetricTaskQueue:
    """Dedicated local DB. Encryption key and DB stay outside all agent workspaces.

    Acceptance is durable before returning. Payloads contain a revocable session digest,
    never a bearer token. Mutable queue state is not an audit or production qualification.
    """

    def __init__(
        self,
        path: Path,
        *,
        encryption_key: bytes,
        clock: Callable[[], datetime] | None = None,
        retention_seconds: int = 86_400,
        lease_seconds: int = 60,
        max_attempts: int = 3,
        max_pending_per_actor: int = 100,
        migrate_v1_offline: bool = False,
    ):
        if not isinstance(encryption_key, bytes) or len(encryption_key) != 32:
            raise ValueError("task_queue_requires_aes256_key")
        for value, lower, upper in (
            (retention_seconds, 1, 7 * 86_400),
            (lease_seconds, 2, 300),
            (max_attempts, 1, 10),
            (max_pending_per_actor, 1, 1_000),
        ):
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError("invalid_task_queue_limits")
        self.path, self.clock = path, clock or (lambda: datetime.now(UTC))
        self.retention_seconds, self.lease_seconds = retention_seconds, lease_seconds
        self.max_attempts, self.max_pending_per_actor = max_attempts, max_pending_per_actor
        self.cipher = AESGCM(encryption_key)
        self.scope_key = hmac.digest(encryption_key, b"gda-metric-task-scope-v1", "sha256")
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("BEGIN EXCLUSIVE" if migrate_v1_offline else "BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if migrate_v1_offline:
                if version != 1:
                    raise ValueError("task_queue_v1_migration_required")
                self._migrate_v1(db)
                version = 2
            if version == 1:
                raise ValueError("task_queue_v1_requires_offline_migration")
            if version not in (0, 2):
                raise ValueError("unsupported_task_queue_schema")
            if (
                version == 0
                and db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1"
                ).fetchone()
            ):
                raise ValueError("dedicated_task_queue_database_required")
            if version == 2:
                columns = [row[1] for row in db.execute("PRAGMA table_info(metric_jobs)")]
                if columns != [
                    "task_id",
                    "scope",
                    "idem",
                    "request_hash",
                    "body",
                    "expires",
                    "status",
                    "attempts",
                    "lease_token",
                    "lease_until",
                    "failure_code",
                    "cancel_requested_at",
                    "cancelled_at",
                ]:
                    raise ValueError("task_queue_v2_layout_unavailable")
            db.execute(
                "CREATE TABLE IF NOT EXISTS metric_jobs ("
                "task_id TEXT PRIMARY KEY, scope TEXT NOT NULL, idem TEXT NOT NULL, "
                "request_hash TEXT NOT NULL, body BLOB NOT NULL, expires REAL NOT NULL, "
                "status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, "
                "lease_token TEXT, lease_until REAL, failure_code TEXT, "
                "cancel_requested_at REAL, cancelled_at REAL, UNIQUE(scope, idem))"
            )
            db.execute("PRAGMA user_version = 2")

    def _migrate_v1(self, db):
        """Explicit offline cutover only: stop ALL API/readers/workers before calling.

        Refuse running jobs even after lease expiry: a lease is not proof of stopped SQL.
        Never migrate automatically, downgrade, or acknowledge an orphaned cancellation.
        """
        expected = [
            "task_id",
            "scope",
            "idem",
            "request_hash",
            "body",
            "expires",
            "status",
            "attempts",
            "lease_token",
            "lease_until",
            "failure_code",
        ]
        columns = [row[1] for row in db.execute("PRAGMA table_info(metric_jobs)")]
        if columns != expected:
            raise ValueError("task_queue_v1_layout_unavailable")
        db.row_factory = sqlite3.Row
        for row in db.execute("SELECT * FROM metric_jobs"):
            if (
                row["status"] not in {"queued", "interrupted", "completed", "failed", "needs_human"}
                or row["lease_token"] is not None
                or row["lease_until"] is not None
            ):
                raise ValueError("task_queue_migration_requires_stopped_jobs")
            self._record(dict(row), self._decode(row))
        db.execute("ALTER TABLE metric_jobs ADD COLUMN cancel_requested_at REAL")
        db.execute("ALTER TABLE metric_jobs ADD COLUMN cancelled_at REAL")
        db.execute("PRAGMA user_version = 2")

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("task_queue_clock_must_be_aware")
        return now

    def _scope(self, actor: ActorContext) -> str:
        return hmac.digest(
            self.scope_key, canonical_actor(actor).model_dump_json().encode(), "sha256"
        ).hex()

    @staticmethod
    def _query_hash(query: MetricQuery) -> str:
        raw = json.dumps(query.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def _aad(row) -> bytes:
        return json.dumps(
            [row["task_id"], row["scope"], row["idem"], row["request_hash"]], separators=(",", ":")
        ).encode()

    def _seal(self, payload: _Payload, row) -> bytes:
        raw = payload.model_dump_json().encode()
        if len(raw) > 65_536:
            raise ValueError("task_request_too_large")
        nonce = os.urandom(12)
        return nonce + self.cipher.encrypt(nonce, raw, self._aad(row))

    def _decode(self, row) -> _Payload:
        try:
            body = row["body"]
            payload = _Payload.model_validate_json(
                self.cipher.decrypt(body[:12], body[12:], self._aad(row))
            )
            if (
                str(payload.task_id) != row["task_id"]
                or self._scope(payload.actor) != row["scope"]
                or self._query_hash(payload.query) != row["request_hash"]
                or payload.expires_at.timestamp() != row["expires"]
            ):
                raise ValueError("binding")
            return payload
        except Exception as exc:
            raise PermissionError("task_queue_record_unavailable") from exc

    @staticmethod
    def _record(row, payload: _Payload) -> MetricTaskRecord:
        return MetricTaskRecord(
            task_id=payload.task_id,
            thread_id=payload.thread_id,
            status=row["status"],
            attempts=row["attempts"],
            created_at=payload.created_at,
            expires_at=payload.expires_at,
            failure_code=row["failure_code"],
            cancel_requested_at=(
                datetime.fromtimestamp(value, UTC)
                if (value := dict(row).get("cancel_requested_at")) is not None
                else None
            ),
            cancelled_at=(
                datetime.fromtimestamp(value, UTC)
                if (value := dict(row).get("cancelled_at")) is not None
                else None
            ),
        )

    @contextmanager
    def _db(self, *, write=False):
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            if db.execute("PRAGMA user_version").fetchone()[0] != 2:
                raise ValueError("unsupported_task_queue_schema")
            yield db

    def submit(
        self, query: MetricQuery, actor: ActorContext, session_reference: str, idempotency_key: str
    ) -> MetricTaskRecord:
        if not isinstance(idempotency_key, str) or not re.fullmatch(
            r"[A-Za-z0-9._:-]{1,128}", idempotency_key
        ):
            raise ValueError("invalid_task_idempotency_key")
        actor = canonical_actor(actor)
        scope, request_hash = self._scope(actor), self._query_hash(query)
        idem = hmac.digest(self.scope_key, idempotency_key.encode(), "sha256").hex()
        now = self._now()
        with self._db(write=True) as db:
            row = db.execute(
                "SELECT * FROM metric_jobs WHERE scope = ? AND idem = ?", (scope, idem)
            ).fetchone()
            if row is not None:
                payload = self._decode(row)
                if row["expires"] <= now.timestamp():
                    raise PermissionError("task_queue_record_unavailable")
                if row["request_hash"] != request_hash:
                    raise TaskQueueConflict("task_idempotency_request_changed")
                return self._record(row, payload)
            pending = db.execute(
                "SELECT COUNT(*) FROM metric_jobs WHERE scope = ? AND expires > ? "
                "AND status IN ('queued', 'running', 'interrupted', 'cancel_requested')",
                (scope, now.timestamp()),
            ).fetchone()[0]
            if pending >= self.max_pending_per_actor:
                raise TaskQueueConflict("task_pending_budget_exhausted")
            payload = _Payload(
                task_id=uuid4(),
                thread_id=uuid4(),
                actor=actor,
                query=query,
                session_reference=session_reference,
                created_at=now,
                expires_at=datetime.fromtimestamp(now.timestamp() + self.retention_seconds, UTC),
            )
            binding = {
                "task_id": str(payload.task_id),
                "scope": scope,
                "idem": idem,
                "request_hash": request_hash,
            }
            db.execute(
                "INSERT INTO metric_jobs "
                "(task_id, scope, idem, request_hash, body, expires, status) "
                "VALUES (?, ?, ?, ?, ?, ?, 'queued')",
                (*binding.values(), self._seal(payload, binding), payload.expires_at.timestamp()),
            )
            return self._record({"status": "queued", "attempts": 0, "failure_code": None}, payload)

    def _owned(self, db, task_id: UUID, actor: ActorContext):
        row = db.execute("SELECT * FROM metric_jobs WHERE task_id = ?", (str(task_id),)).fetchone()
        if row is None or row["expires"] <= self._now().timestamp():
            raise PermissionError("task_queue_record_unavailable")
        payload = self._decode(row)
        owner = payload.actor
        if (owner.user_id, owner.tenant_id, owner.project_id) != (
            actor.user_id,
            actor.tenant_id,
            actor.project_id,
        ) or not set(owner.data_domains) <= set(actor.data_domains):
            raise PermissionError("task_queue_record_unavailable")
        return row, payload

    def read(self, task_id: UUID, actor: ActorContext) -> MetricTaskRecord:
        with self._db() as db:
            return self._record(*self._owned(db, task_id, actor))

    def result_context(self, task_id: UUID, actor: ActorContext) -> MetricTaskContext:
        """Read-only request binding, without exposing a stored grant or claiming execution."""
        with self._db() as db:
            row, payload = self._owned(db, task_id, actor)
            record = self._record(row, payload)
            if record.status != "completed":
                raise TaskQueueConflict("task_result_not_ready")
            return MetricTaskContext(record, payload.actor, payload.query)

    def _encode_cursor(self, cursor: _ListCursor) -> str:
        nonce = os.urandom(12)
        raw = nonce + self.cipher.encrypt(
            nonce, cursor.model_dump_json().encode(), b"gda-task-list-cursor-v1"
        )
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    def _decode_cursor(self, value: str, scope: str, now: datetime) -> _ListCursor:
        try:
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,2048}", value):
                raise ValueError("encoding")
            raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            cursor = _ListCursor.model_validate_json(
                self.cipher.decrypt(raw[:12], raw[12:], b"gda-task-list-cursor-v1")
            )
            if cursor.scope != scope or cursor.expires_at <= now or cursor.after >= cursor.upper:
                raise ValueError("scope_or_expiry")
            return cursor
        except Exception as exc:
            raise TaskQueueConflict("task_list_cursor_invalid") from exc

    def list_tasks(
        self, actor: ActorContext, *, limit: int = 50, cursor: str | None = None
    ) -> MetricTaskPage:
        """Exact domain-view listing, with an opaque scope-bound five-minute cursor.

        A fixed upper row and identity exclude new submissions. Removing/replacing that
        anchor invalidates the cursor, preventing SQLite rowid reuse from admitting new work.
        """
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid_task_list_limit")
        now, scope = self._now(), self._scope(actor)
        bound = self._decode_cursor(cursor, scope, now) if cursor is not None else None
        with self._db() as db:
            if bound is None:
                anchor = db.execute(
                    "SELECT rowid, task_id FROM metric_jobs WHERE scope = ? AND expires > ? "
                    "ORDER BY rowid DESC LIMIT 1",
                    (scope, now.timestamp()),
                ).fetchone()
                if anchor is None:
                    return MetricTaskPage(tasks=())
                bound = _ListCursor(
                    scope=scope,
                    after=0,
                    upper=anchor["rowid"],
                    anchor=anchor["task_id"],
                    expires_at=datetime.fromtimestamp(now.timestamp() + 300, UTC),
                )
            elif (
                db.execute(
                    "SELECT 1 FROM metric_jobs WHERE rowid = ? AND task_id = ? AND scope = ?",
                    (bound.upper, str(bound.anchor), scope),
                ).fetchone()
                is None
            ):
                raise TaskQueueConflict("task_list_cursor_invalid")
            rows = db.execute(
                "SELECT rowid, * FROM metric_jobs WHERE scope = ? AND expires > ? "
                "AND rowid > ? AND rowid <= ? ORDER BY rowid LIMIT ?",
                (scope, now.timestamp(), bound.after, bound.upper, limit + 1),
            ).fetchall()
            tasks = tuple(self._record(row, self._decode(row)) for row in rows[:limit])
            next_cursor = None
            if len(rows) > limit:
                next_cursor = self._encode_cursor(
                    bound.model_copy(update={"after": rows[limit - 1]["rowid"]})
                )
            return MetricTaskPage(tasks=tasks, next_cursor=next_cursor)

    def retry(self, task_id: UUID, actor: ActorContext, session_reference: str) -> MetricTaskRecord:
        with self._db(write=True) as db:
            row, payload = self._owned(db, task_id, actor)
            if row["status"] != "interrupted" or row["attempts"] >= self.max_attempts:
                raise TaskQueueConflict("task_is_not_retryable")
            payload = _Payload.model_validate(
                payload.model_dump() | {"session_reference": session_reference}
            )
            db.execute(
                "UPDATE metric_jobs SET body = ?, status = 'queued', failure_code = NULL "
                "WHERE task_id = ?",
                (self._seal(payload, row), str(task_id)),
            )
            return self._record(
                {"status": "queued", "attempts": row["attempts"], "failure_code": None}, payload
            )

    def cancel(self, task_id: UUID, actor: ActorContext) -> MetricTaskRecord:
        with self._db(write=True) as db:
            row, payload = self._owned(db, task_id, actor)
            if row["status"] in {"cancel_requested", "cancelled"}:
                return self._record(row, payload)
            if row["status"] not in {"queued", "interrupted", "running"}:
                raise TaskQueueConflict("task_is_not_cancellable")
            now = self._now().timestamp()
            # Only an executing lease holder can acknowledge a running task's stop.
            status = "cancel_requested" if row["status"] == "running" else "cancelled"
            db.execute(
                "UPDATE metric_jobs SET status = ?, cancel_requested_at = ?, "
                "cancelled_at = ?, failure_code = NULL WHERE task_id = ?",
                (status, now, now if status == "cancelled" else None, str(task_id)),
            )
            updated = db.execute(
                "SELECT * FROM metric_jobs WHERE task_id = ?", (str(task_id),)
            ).fetchone()
            return self._record(updated, payload)

    def claim(self) -> MetricTaskClaim | None:
        now = self._now().timestamp()
        with self._db(write=True) as db:
            row = db.execute(
                "SELECT * FROM metric_jobs WHERE expires > ? AND (status = 'queued' "
                "OR (status = 'running' AND lease_until <= ?)) ORDER BY rowid LIMIT 1",
                (now, now),
            ).fetchone()
            if row is None:
                return None
            payload = self._decode(row)
            if row["attempts"] >= self.max_attempts:
                db.execute(
                    "UPDATE metric_jobs SET status = 'failed', failure_code = "
                    "'attempts_exhausted', lease_token = NULL, lease_until = NULL "
                    "WHERE task_id = ?",
                    (row["task_id"],),
                )
                return None
            token = str(uuid4())
            db.execute(
                "UPDATE metric_jobs SET status = 'running', attempts = attempts + 1, "
                "lease_token = ?, lease_until = ?, failure_code = NULL WHERE task_id = ?",
                (token, now + self.lease_seconds, row["task_id"]),
            )
            record = self._record(
                {"status": "running", "attempts": row["attempts"] + 1, "failure_code": None},
                payload,
            )
            return MetricTaskClaim(
                record, payload.actor, payload.query, payload.session_reference, token
            )

    def _leased(self, db, claim: MetricTaskClaim):
        row = db.execute(
            "SELECT * FROM metric_jobs WHERE task_id = ?", (str(claim.record.task_id),)
        ).fetchone()
        now = self._now().timestamp()
        if (
            row is None
            or row["status"] not in {"running", "cancel_requested"}
            or row["lease_token"] != claim.lease_token
            or row["lease_until"] <= now
            or row["expires"] <= now
        ):
            raise TaskLeaseLost("task_worker_lease_lost")
        payload = self._decode(row)
        if (
            payload.actor != claim.actor
            or payload.query != claim.query
            or payload.thread_id != claim.record.thread_id
            or payload.session_reference != claim.session_reference
        ):
            raise TaskLeaseLost("task_worker_claim_changed")
        return row, payload

    def check(self, claim: MetricTaskClaim) -> None:
        with self._db() as db:
            row, _ = self._leased(db, claim)
            if row["status"] == "cancel_requested":
                raise TaskCancellationRequested("task_cancellation_requested")

    def renew(self, claim: MetricTaskClaim) -> None:
        with self._db(write=True) as db:
            self._leased(db, claim)
            db.execute(
                "UPDATE metric_jobs SET lease_until = ? WHERE task_id = ?",
                (self._now().timestamp() + self.lease_seconds, str(claim.record.task_id)),
            )

    def settle(
        self,
        claim: MetricTaskClaim,
        status: Literal["interrupted", "completed", "failed", "needs_human"],
        failure_code: FailureCode | None = None,
    ) -> MetricTaskRecord:
        if status not in {"interrupted", "completed", "failed", "needs_human"}:
            raise ValueError("invalid_task_settlement")
        with self._db(write=True) as db:
            row, payload = self._leased(db, claim)
            cancelled_at = None
            if row["status"] == "cancel_requested":
                status, failure_code = "cancelled", None
                cancelled_at = self._now().timestamp()
            if status == "interrupted" and row["attempts"] >= self.max_attempts:
                status, failure_code = "failed", "attempts_exhausted"
            record = self._record(
                dict(row)
                | {"status": status, "failure_code": failure_code, "cancelled_at": cancelled_at},
                payload,
            )
            db.execute(
                "UPDATE metric_jobs SET status = ?, failure_code = ?, lease_token = NULL, "
                "lease_until = NULL, cancelled_at = ? WHERE task_id = ?",
                (status, failure_code, cancelled_at, row["task_id"]),
            )
            return record

    @contextmanager
    def execution(self, claim: MetricTaskClaim) -> Iterator[Callable[[], None]]:
        stopped, lost = Event(), Event()

        def heartbeat():
            while not stopped.wait(min(5, self.lease_seconds / 3)):
                try:
                    self.renew(claim)
                except Exception:
                    lost.set()
                    return

        def guard():
            if lost.is_set():
                raise TaskLeaseLost("task_worker_lease_lost")
            self.check(claim)

        guard()
        thread = Thread(target=heartbeat, name="gda-task-lease", daemon=True)
        thread.start()
        try:
            yield guard
            guard()
        finally:
            stopped.set()
            thread.join(timeout=1)

    def purge_expired(self) -> int:
        with self._db(write=True) as db:
            rows = db.execute(
                "SELECT task_id FROM metric_jobs WHERE expires <= ? LIMIT 1000",
                (self._now().timestamp(),),
            ).fetchall()
            db.executemany(
                "DELETE FROM metric_jobs WHERE task_id = ?", [(row["task_id"],) for row in rows]
            )
            return len(rows)
