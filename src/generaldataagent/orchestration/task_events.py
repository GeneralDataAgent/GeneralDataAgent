"""Durable, creator-scoped progress metadata for local task consumers.

This is an application event protocol, not the LangGraph wire protocol or an audit sink.
Only fixed metadata may cross this boundary; SQL, prompts and result rows never do.
"""

import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from generaldataagent.contracts import ActorContext
from generaldataagent.contracts.models import ContractModel

EventKind = Literal["started", "phase", "interrupted", "finished"]
TaskPhase = Literal["discover", "plan", "analyze", "validate", "deliver"]
TaskOutcome = Literal["completed", "failed", "needs_human"]


class TaskEvent(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    task_id: UUID
    sequence: int = Field(ge=1, strict=True)
    kind: EventKind
    phase: TaskPhase | None = None
    outcome: TaskOutcome | None = None
    created_at: AwareDatetime

    @model_validator(mode="after")
    def coherent(self):
        if (self.kind == "phase") != (self.phase is not None):
            raise ValueError("task_event_phase_mismatch")
        if (self.kind == "finished") != (self.outcome is not None):
            raise ValueError("task_event_outcome_mismatch")
        return self


class TaskEventPage(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    task_id: UUID
    events: tuple[TaskEvent, ...]
    next_cursor: int = Field(ge=0)
    latest_sequence: int = Field(ge=1)
    has_more: bool
    terminal: bool


class TaskEventJournal:
    """Local SQLite journal; operators own filesystem access, retention and backups.

    Each mutation uses BEGIN IMMEDIATE so independent processes share one sequence.
    Expiry is checked on every access. This adapter is not production-qualified.
    """

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        retention_seconds: int = 7 * 86_400,
        max_events_per_task: int = 1_000,
    ) -> None:
        if type(retention_seconds) is not int or not 1 <= retention_seconds <= 30 * 86_400:
            raise ValueError("invalid_task_event_retention")
        if type(max_events_per_task) is not int or not 2 <= max_events_per_task <= 10_000:
            raise ValueError("invalid_task_event_budget")
        self.path = path
        self.clock = clock or (lambda: datetime.now(UTC))
        self.retention_seconds = retention_seconds
        self.max_events_per_task = max_events_per_task
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("unsupported_task_event_schema")
            if (
                version == 0
                and db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1"
                ).fetchone()
            ):
                raise ValueError("dedicated_task_event_database_required")
            db.execute(
                "CREATE TABLE IF NOT EXISTS event_tasks ("
                "task_id TEXT PRIMARY KEY, actor TEXT NOT NULL, expires REAL NOT NULL, "
                "latest INTEGER NOT NULL, terminal INTEGER NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS task_events ("
                "task_id TEXT NOT NULL, sequence INTEGER NOT NULL, body TEXT NOT NULL, "
                "PRIMARY KEY (task_id, sequence))"
            )
            db.execute("PRAGMA user_version = 1")

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("task_event_clock_must_be_aware")
        return now

    @staticmethod
    def _authorize(db, task_id: UUID, actor: ActorContext, timestamp: float):
        row = db.execute(
            "SELECT actor, expires, latest, terminal FROM event_tasks WHERE task_id = ?",
            (str(task_id),),
        ).fetchone()
        if row is None or row[1] <= timestamp:
            raise PermissionError("task_events_unavailable")
        owner = ActorContext.model_validate_json(row[0])
        if (owner.user_id, owner.tenant_id, owner.project_id) != (
            actor.user_id,
            actor.tenant_id,
            actor.project_id,
        ) or not set(owner.data_domains) <= set(actor.data_domains):
            raise PermissionError("task_events_unavailable")
        return row

    def start(self, task_id: UUID, actor: ActorContext) -> TaskEvent:
        task_id = UUID(str(task_id))
        now = self._now()
        event = TaskEvent(task_id=task_id, sequence=1, kind="started", created_at=now)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM event_tasks WHERE task_id = ?", (str(task_id),)
            ).fetchone():
                # Never silently rebind or restart a task, even after its retention expires.
                raise ValueError("task_event_task_already_exists")
            db.execute(
                "INSERT INTO event_tasks VALUES (?, ?, ?, 1, 0)",
                (str(task_id), actor.model_dump_json(), now.timestamp() + self.retention_seconds),
            )
            db.execute(
                "INSERT INTO task_events VALUES (?, 1, ?)",
                (str(task_id), event.model_dump_json()),
            )
        return event

    def append(
        self,
        task_id: UUID,
        actor: ActorContext,
        kind: Literal["phase", "interrupted", "finished"],
        *,
        phase: TaskPhase | None = None,
        outcome: TaskOutcome | None = None,
    ) -> TaskEvent:
        task_id = UUID(str(task_id))
        now = self._now()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            _, _, latest, terminal = self._authorize(db, task_id, actor, now.timestamp())
            if terminal:
                raise ValueError("task_event_task_is_terminal")
            if latest >= self.max_events_per_task:
                raise ValueError("task_event_budget_exhausted")
            event = TaskEvent(
                task_id=task_id,
                sequence=latest + 1,
                kind=kind,
                phase=phase,
                outcome=outcome,
                created_at=now,
            )
            if event.kind == "started":
                raise ValueError("task_event_start_requires_registration")
            db.execute(
                "INSERT INTO task_events VALUES (?, ?, ?)",
                (str(task_id), event.sequence, event.model_dump_json()),
            )
            db.execute(
                "UPDATE event_tasks SET latest = ?, terminal = ? WHERE task_id = ?",
                (event.sequence, int(event.kind == "finished"), str(task_id)),
            )
        return event

    def ensure_started(self, task_id: UUID, actor: ActorContext) -> TaskEvent:
        """Recover registration without rebinding ownership or extending retention."""
        try:
            return self.start(task_id, actor)
        except ValueError as exc:
            if str(exc) != "task_event_task_already_exists":
                raise
        page = self.read(task_id, actor, limit=1)
        event = page.events[0]
        if event.kind != "started" or event.sequence != 1:
            raise ValueError("task_event_registration_corrupt")
        return event

    def read(
        self, task_id: UUID, actor: ActorContext, *, after: int = 0, limit: int = 100
    ) -> TaskEventPage:
        task_id = UUID(str(task_id))
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("invalid_task_event_cursor")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN")
            _, _, latest, terminal = self._authorize(db, task_id, actor, self._now().timestamp())
            if after > latest:
                raise ValueError("task_event_cursor_ahead")
            rows = db.execute(
                "SELECT body FROM task_events WHERE task_id = ? AND sequence > ? "
                "ORDER BY sequence LIMIT ?",
                (str(task_id), after, limit),
            ).fetchall()
            events = tuple(TaskEvent.model_validate_json(row[0]) for row in rows)
            cursor = events[-1].sequence if events else after
            if (
                [e.sequence for e in events] != list(range(after + 1, cursor + 1))
                or any(e.task_id != task_id for e in events)
                or (cursor < latest and len(events) < limit)
            ):
                raise ValueError("task_event_sequence_corrupt")
            return TaskEventPage(
                task_id=task_id,
                events=events,
                next_cursor=cursor,
                latest_sequence=latest,
                has_more=cursor < latest,
                terminal=bool(terminal),
            )

    def purge_expired(self) -> int:
        """Operator-only cleanup, never exposed as an agent or HTTP tool."""
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            ids = db.execute(
                "SELECT task_id FROM event_tasks WHERE expires <= ? LIMIT 1000",
                (self._now().timestamp(),),
            ).fetchall()
            db.executemany("DELETE FROM task_events WHERE task_id = ?", ids)
            db.executemany("DELETE FROM event_tasks WHERE task_id = ?", ids)
        return len(ids)
