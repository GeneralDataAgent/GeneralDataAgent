"""Durable claims and evidence references for one-shot XLSX quality assessments."""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from generaldataagent.contracts import ArtifactRef
from generaldataagent.datasets.xlsx_intake_store import XlsxIntakeConflict, XlsxIntakeUnavailable


@dataclass(frozen=True)
class QualityIntent:
    task_id: UUID
    state: Literal["pending", "failed", "completed"]
    evidence: ArtifactRef | None


class XlsxQualityIntents:
    """Use the protected quality index, without changing its artifact tables."""

    def __init__(self, index_path: Path):
        self.path = index_path
        index_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS xlsx_quality_intents ("
                "scope TEXT NOT NULL, intent TEXT NOT NULL, file_id TEXT NOT NULL, "
                "snapshot_hash TEXT NOT NULL, task_id TEXT NOT NULL, expires REAL NOT NULL, "
                "state TEXT NOT NULL CHECK(state IN ('pending','failed','completed')), "
                "evidence TEXT, PRIMARY KEY(scope,intent))"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS xlsx_quality_intents_expiry "
                "ON xlsx_quality_intents(expires)"
            )

    @staticmethod
    def _lookup(db, scope, intent, file_id, snapshot_hash) -> QualityIntent | None:
        row = db.execute(
            "SELECT file_id, snapshot_hash, task_id, state, evidence "
            "FROM xlsx_quality_intents WHERE scope=? AND intent=?",
            (scope, intent),
        ).fetchone()
        if row is None:
            return None
        if row[:2] != (str(file_id), snapshot_hash):
            raise XlsxIntakeConflict("xlsx_quality_intent_request_changed")
        task_id, state, evidence = UUID(row[2]), row[3], row[4]
        if state not in ("pending", "failed", "completed") or (state == "completed") != bool(
            evidence
        ):
            raise XlsxIntakeUnavailable("xlsx_quality_intent_unavailable")
        ref = ArtifactRef.model_validate_json(evidence) if evidence else None
        if ref is not None and ref.task_id != task_id:
            raise XlsxIntakeUnavailable("xlsx_quality_intent_unavailable")
        return QualityIntent(task_id, state, ref)

    def lookup(self, scope, intent, file_id, snapshot_hash) -> QualityIntent | None:
        with closing(sqlite3.connect(self.path)) as db:
            return self._lookup(db, scope, intent, file_id, snapshot_hash)

    def claim(self, scope, intent, file_id, snapshot_hash, *, now, expires):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM xlsx_quality_intents WHERE expires<=?", (now,))
            existing = self._lookup(db, scope, intent, file_id, snapshot_hash)
            if existing is not None:
                return existing, False
            task_id = uuid4()
            db.execute(
                "INSERT INTO xlsx_quality_intents VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL)",
                (scope, intent, str(file_id), snapshot_hash, str(task_id), expires),
            )
            return QualityIntent(task_id, "pending", None), True

    def settle(self, scope, intent, task_id, evidence: ArtifactRef | None) -> None:
        with closing(sqlite3.connect(self.path)) as db, db:
            changed = db.execute(
                "UPDATE xlsx_quality_intents SET state=?, evidence=? "
                "WHERE scope=? AND intent=? AND task_id=? AND state='pending'",
                (
                    "completed" if evidence is not None else "failed",
                    evidence.model_dump_json() if evidence is not None else None,
                    scope,
                    intent,
                    str(task_id),
                ),
            ).rowcount
            if changed != 1:
                raise XlsxIntakeUnavailable("xlsx_quality_intent_unavailable")
