"""Restart-safe, append-only local experiment tracking.

An external experiment index, when one exists, is a mirror: the local,
integrity-checked record is the evidence, and the mirror can only lose its own
copy of it. That asymmetry is deliberate. A service that is down must not be
able to turn a recorded experiment into a failed one, and must not be able to
make an unrecorded experiment look delivered.
"""

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Protocol
from uuid import UUID

from generaldataagent.contracts import ExperimentReport, ExperimentSpec, TrackedExperiment


class ExperimentTrackingError(RuntimeError):
    pass


class ExperimentMirror(Protocol):
    """External experiment index. It is never the record of authority."""

    backend_name: str

    def admitted(self) -> bool: ...
    def publish(self, tracked: TrackedExperiment) -> str: ...


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


class MirroringExperimentTracker:
    """Write the authoritative record first, then try to mirror it.

    A mirror failure never changes the returned record and never rolls the local
    write back: the experiment happened, and the operator's copy is complete even
    when the service is unreachable. What a failure does change is visibility —
    the record is marked ``pending`` with an attempt count and the error, and
    ``publish_pending`` retries it. ``mirror_status`` is the only place the
    mirror state is reported; a delivered mirror is not evidence that an
    experiment ran.
    """

    def __init__(self, local: SQLiteExperimentTracker, mirror: ExperimentMirror) -> None:
        if not isinstance(local, SQLiteExperimentTracker):
            raise ValueError("the local tracker is the record of authority")
        if not hasattr(mirror, "backend_name") or not mirror.backend_name:
            raise ValueError("an experiment mirror needs a backend name")
        if mirror.admitted() is not True:
            raise ValueError("experiment mirror is not admitted")
        self.local = local
        self.mirror = mirror
        with closing(sqlite3.connect(local.path, timeout=5)) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS experiment_mirror (
                    experiment_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    last_error TEXT
                )
                """
            )

    def record(self, *, spec: ExperimentSpec, report: ExperimentReport) -> TrackedExperiment:
        tracked = self.local.record(spec=spec, report=report)
        self._publish(tracked)
        return tracked

    def get(self, experiment_id: UUID) -> TrackedExperiment:
        return self.local.get(experiment_id)

    def list_for_snapshot(self, snapshot_id: UUID) -> tuple[TrackedExperiment, ...]:
        return self.local.list_for_snapshot(snapshot_id)

    def mirror_status(self, experiment_id: UUID) -> dict:
        with closing(sqlite3.connect(self.local.path, timeout=5)) as connection:
            row = connection.execute(
                "SELECT status, attempts, last_error FROM experiment_mirror "
                "WHERE experiment_id = ?",
                (str(experiment_id),),
            ).fetchone()
        if row is None:
            return {"status": "not_attempted", "attempts": 0, "last_error": None}
        return {"status": row[0], "attempts": row[1], "last_error": row[2]}

    def publish_pending(self) -> int:
        """Retry every undelivered mirror write; return how many were delivered."""
        with closing(sqlite3.connect(self.local.path, timeout=5)) as connection:
            pending = [
                row[0]
                for row in connection.execute(
                    "SELECT experiment_id FROM experiment_mirror WHERE status = 'pending' "
                    "ORDER BY experiment_id"
                ).fetchall()
            ]
        delivered = 0
        for experiment_id in pending:
            if self._publish(self.local.get(UUID(experiment_id))):
                delivered += 1
        return delivered

    def _publish(self, tracked: TrackedExperiment) -> bool:
        try:
            self.mirror.publish(tracked)
        except Exception as exc:
            self._mark(tracked.experiment_id, "pending", self._error(exc))
            return False
        self._mark(tracked.experiment_id, "delivered", None)
        return True

    def _mark(self, experiment_id: UUID, status: str, error: str | None) -> None:
        with closing(sqlite3.connect(self.local.path, timeout=5)) as connection, connection:
            connection.execute(
                """
                INSERT INTO experiment_mirror (experiment_id, status, attempts, last_error)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(experiment_id) DO UPDATE SET
                    status = excluded.status,
                    attempts = experiment_mirror.attempts + 1,
                    last_error = excluded.last_error
                """,
                (str(experiment_id), status, error),
            )

    @staticmethod
    def _error(exc: Exception) -> str:
        return f"{type(exc).__name__}: {exc}"[:200]
