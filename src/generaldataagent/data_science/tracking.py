"""Restart-safe, append-only local experiment tracking."""

import hashlib
import sqlite3
from pathlib import Path
from uuid import UUID

from generaldataagent.contracts import ExperimentReport, ExperimentSpec, TrackedExperiment


class ExperimentTrackingError(RuntimeError):
    pass


class SQLiteExperimentTracker:
    """Stores immutable experiment records and verifies them on every read."""

    def __init__(self, path: Path) -> None:
        if path.exists() and not path.is_file():
            raise ValueError("experiment tracker path must be a file")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS experiments (
                    experiment_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL
                )
                """
            )

    def record(self, *, spec: ExperimentSpec, report: ExperimentReport) -> TrackedExperiment:
        tracked = TrackedExperiment(
            experiment_id=spec.experiment_id,
            task_id=spec.task_id,
            snapshot_id=spec.snapshot_id,
            spec=spec,
            report=report,
        )
        content = tracked.model_dump_json()
        content_hash = _hash(content)
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO experiments (
                        experiment_id, task_id, snapshot_id, record_json, content_hash
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        str(tracked.experiment_id),
                        str(tracked.task_id),
                        str(tracked.snapshot_id),
                        content,
                        content_hash,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ExperimentTrackingError("experiment id is already recorded") from exc
        return tracked

    def get(self, experiment_id: UUID) -> TrackedExperiment:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT record_json, content_hash FROM experiments WHERE experiment_id = ?",
                (str(experiment_id),),
            ).fetchone()
        if row is None:
            raise ExperimentTrackingError("experiment was not found")
        content, expected_hash = row
        if _hash(content) != expected_hash:
            raise ExperimentTrackingError("experiment tracking record failed integrity check")
        return TrackedExperiment.model_validate_json(content)

    def list_for_snapshot(self, snapshot_id: UUID) -> tuple[TrackedExperiment, ...]:
        with self._connect() as connection:
            identifiers = connection.execute(
                """
                SELECT experiment_id FROM experiments
                WHERE snapshot_id = ? ORDER BY experiment_id
                """,
                (str(snapshot_id),),
            ).fetchall()
        return tuple(self.get(UUID(row[0])) for row in identifiers)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection


def _hash(content: str) -> str:
    return f"sha256:{hashlib.sha256(content.encode()).hexdigest()}"
