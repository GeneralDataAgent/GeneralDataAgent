"""Bounded periodic report draft generation; each occurrence needs its own approval."""

import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid5

from pydantic import AwareDatetime, Field, model_validator

from generaldataagent.business.actions import BusinessActionRuntime
from generaldataagent.business.report_refresh import MetricReportRefresher
from generaldataagent.contracts import ActorContext, MetricQuery
from generaldataagent.contracts.business import ActionIntent, ReportDraft
from generaldataagent.contracts.models import ContractModel


class ReportSchedule(ContractModel):
    schedule_id: UUID
    destination: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    report: ReportDraft
    starts_at: AwareDatetime
    interval_minutes: int = Field(ge=60, le=525600)
    occurrences: int = Field(ge=1, le=100)
    initial_revision: int = Field(default=0, ge=0)
    revision_policy: Literal["sequential", "current_at_proposal"] = "sequential"
    query: MetricQuery | None = None
    lookback_minutes: int | None = Field(default=None, ge=1, le=525600)

    @model_validator(mode="after")
    def validate_rolling_window(self):
        if self.lookback_minutes is not None and (
            self.query is None
            or self.query.start_time is not None
            or self.query.end_time is not None
        ):
            raise ValueError("rolling reports require a query without fixed time bounds")
        return self


class ReportScheduler:
    def __init__(
        self,
        path: Path,
        *,
        actions: BusinessActionRuntime,
        refresher: MetricReportRefresher | None = None,
    ):
        self.path, self.actions, self.refresher = path, actions, refresher
        if refresher is not None and refresher.path.resolve() in {
            path.resolve(),
            actions.path.resolve(),
        }:
            raise ValueError("report generation metadata needs a separate protected database")
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript(
                "CREATE TABLE IF NOT EXISTS business_schedules ("
                "id TEXT PRIMARY KEY, owner TEXT, tenant TEXT, project TEXT, spec TEXT, "
                "next_index INTEGER, enabled INTEGER);"
            )

    def register(self, spec: ReportSchedule, token: str) -> dict:
        identity = self.actions._identity(token, "business:propose")
        self.actions._control(self._intent(spec, 0), identity)
        if spec.query is not None and (
            self.refresher is None or self.actions.artifact_reader is None
        ):
            raise ValueError("fresh reports require a refresher and a scoped artifact reader")
        self.actions.scanner.check(spec.model_dump_json().encode())
        if spec.starts_at < self.actions._now():
            raise ValueError("schedule_must_start_in_future")
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            try:
                db.execute(
                    "INSERT INTO business_schedules VALUES(?,?,?,?,?,0,1)",
                    (
                        str(spec.schedule_id),
                        identity.user_id,
                        identity.tenant_id,
                        identity.project_id,
                        spec.model_dump_json(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("schedule_already_exists") from exc
        return {"schedule_id": str(spec.schedule_id), "state": "active"}

    @staticmethod
    def _intent(spec, index):
        due = spec.starts_at + timedelta(minutes=spec.interval_minutes * index)
        return ActionIntent(
            action_id=uuid5(spec.schedule_id, f"action:{index}"),
            task_id=uuid5(spec.schedule_id, f"task:{index}"),
            thread_id=f"schedule:{spec.schedule_id}:{index}",
            idempotency_key=f"schedule:{spec.schedule_id}:{index}",
            destination=spec.destination,
            expected_revision=spec.initial_revision + index,
            payload=spec.report.model_copy(update={"period": due.isoformat()}),
            created_at=due,
            expires_at=due + timedelta(minutes=min(spec.interval_minutes, 1440)),
        )

    def _proposal_intent(self, spec, intent, identity):
        if spec.revision_policy == "sequential":
            return intent
        with self.actions._db() as db:
            # If a proposal committed before a scheduler crash, keep its exact approval binding.
            existing = db.execute(
                "SELECT id FROM business_actions WHERE id=?", (str(intent.action_id),)
            ).fetchone()
            if existing is not None:
                _, original, _ = self.actions._row(db, existing[0], identity)
                return original
            resource = db.execute(
                "SELECT revision FROM business_resources "
                "WHERE tenant=? AND project=? AND destination=?",
                (identity.tenant_id, identity.project_id, intent.destination),
            ).fetchone()
        revision = resource[0] if resource is not None else spec.initial_revision
        # This is a NEW draft, before approval. Execution still rejects later resource changes.
        return intent.model_copy(update={"expected_revision": revision})

    def _row(self, db, schedule_id, identity):
        row = db.execute(
            "SELECT owner,tenant,project,spec,next_index,enabled "
            "FROM business_schedules WHERE id=?",
            (str(schedule_id),),
        ).fetchone()
        if row is None or row[:3] != (identity.user_id, identity.tenant_id, identity.project_id):
            raise PermissionError("schedule_access_denied")
        spec = ReportSchedule.model_validate_json(row[3])
        self.actions._control(self._intent(spec, 0), identity)
        return row, spec

    def cancel(self, schedule_id: UUID, token: str) -> dict:
        identity = self.actions._identity(token, "business:propose")
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            self._row(db, schedule_id, identity)
            db.execute("UPDATE business_schedules SET enabled=0 WHERE id=?", (str(schedule_id),))
        return {"schedule_id": str(schedule_id), "state": "cancelled"}

    def tick(self, schedule_id: UUID, token: str, *, limit: int = 10) -> dict:
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError("invalid_tick_limit")
        identity = self.actions._identity(token, "business:propose")
        emitted, expired = [], []
        index = 0
        for _ in range(limit):
            with closing(sqlite3.connect(self.path, timeout=5)) as db:
                row, spec = self._row(db, schedule_id, identity)
                if not row[5]:
                    raise PermissionError("schedule_disabled")
                index = row[4]
                if index >= spec.occurrences:
                    break
                intent = self._intent(spec, index)
                now = self.actions._now()
                if intent.created_at > now:
                    break
            # Query/model work runs outside the schedule transaction, so cancellation is available.
            if intent.expires_at > now and spec.query is not None:
                if self.refresher is None:
                    raise ValueError("report_refresher_unavailable")
                query = spec.query
                if spec.lookback_minutes is not None:
                    query = query.model_copy(
                        update={
                            "start_time": intent.created_at
                            - timedelta(minutes=spec.lookback_minutes),
                            "end_time": intent.created_at,
                        }
                    )
                control = self.actions._control(intent, identity)
                actor = ActorContext(
                    user_id=identity.user_id,
                    tenant_id=identity.tenant_id,
                    project_id=identity.project_id,
                    data_domains=tuple(sorted(control.data_domains)),
                )
                ref = self.refresher.prepare(
                    schedule_id=spec.schedule_id, index=index, query=query, actor=actor
                )
                report = intent.payload.model_copy(
                    update={"report_hash": ref.content_hash, "report_artifact": ref}
                )
                intent = intent.model_copy(update={"payload": report})
            identity = self.actions._identity(token, "business:propose")
            with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                current, _ = self._row(db, schedule_id, identity)
                if not current[5]:
                    raise PermissionError("schedule_disabled")
                if current[4] != index:
                    continue
                if intent.expires_at <= self.actions._now():
                    expired.append(index)
                else:
                    intent = self._proposal_intent(spec, intent, identity)
                    emitted.append(self.actions.propose(intent, token))
                index += 1
                db.execute(
                    "UPDATE business_schedules SET next_index=? WHERE id=?",
                    (index, str(schedule_id)),
                )
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            index = db.execute(
                "SELECT next_index FROM business_schedules WHERE id=?", (str(schedule_id),)
            ).fetchone()[0]
        return {
            "schedule_id": str(schedule_id),
            "drafts": emitted,
            "expired": expired,
            "next_index": index,
            "complete": index == spec.occurrences,
        }
