"""Service-owned alert retries and retention sweeps, independent of new user requests."""

from datetime import UTC, datetime
from threading import Event, Lock, Thread
from typing import Any

from generaldataagent.artifacts.governed import GovernedArtifactStore
from generaldataagent.observability.alerts import CriticalAlertRouter
from generaldataagent.orchestration.checkpoints import ProtectedSqliteSaver


class ServiceMaintenance:
    """Maintain only explicitly configured stores and transports."""

    def __init__(
        self,
        *,
        artifacts: tuple[GovernedArtifactStore, ...] = (),
        checkpoints: tuple[ProtectedSqliteSaver, ...] = (),
        alerts: CriticalAlertRouter | None = None,
        interval_seconds: int = 30,
    ):
        if type(interval_seconds) is not int or not 1 <= interval_seconds <= 60:
            raise ValueError("maintenance interval must be between 1 and 60 seconds")
        self.artifacts, self.checkpoints, self.alerts = artifacts, checkpoints, alerts
        self.interval_seconds = interval_seconds
        self._stop, self._lock, self._tick_lock = Event(), Lock(), Lock()
        self._thread: Thread | None = None
        self._status: dict[str, Any] = {
            "runs": 0,
            "last_run": None,
            "failures": 0,
            "artifacts_deleted": 0,
            "checkpoints_deleted": 0,
            "alerts_delivered": 0,
        }

    def tick(self) -> dict[str, Any]:
        with self._tick_lock:
            counts = {
                "failures": 0,
                "artifacts_deleted": 0,
                "checkpoints_deleted": 0,
                "alerts_delivered": 0,
            }
            if self.alerts is not None and not self._stop.is_set():
                try:
                    result = self.alerts.retry_pending(should_stop=self._stop.is_set)
                    counts["alerts_delivered"] = result["delivered"]
                    counts["failures"] += result["failed"]
                except Exception:
                    counts["failures"] += 1
            for store in self.artifacts:
                if self._stop.is_set():
                    break
                try:
                    counts["artifacts_deleted"] += len(store.delete_expired())
                except Exception:
                    counts["failures"] += 1
            for saver in self.checkpoints:
                if self._stop.is_set():
                    break
                try:
                    counts["checkpoints_deleted"] += saver.purge_expired()
                except Exception:
                    counts["failures"] += 1
            with self._lock:
                self._status["runs"] += 1
                self._status["last_run"] = datetime.now(UTC).isoformat()
                for key, value in counts.items():
                    self._status[key] += value
                return dict(self._status)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {**self._status, "running": self._thread is not None and self._thread.is_alive()}

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            raise ValueError("maintenance already running")
        self._stop.clear()

        def run():
            while not self._stop.is_set():
                self.tick()
                self._stop.wait(self.interval_seconds)

        self._thread = Thread(target=run, name="gda-service-maintenance", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=30)
            if self._thread.is_alive():
                raise RuntimeError("maintenance_shutdown_incomplete")
