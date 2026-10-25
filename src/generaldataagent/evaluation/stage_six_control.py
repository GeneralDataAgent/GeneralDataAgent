"""Shared per-run service circuit breaker; failed/not-attempted tasks remain in the cohort."""

from threading import Lock


class ServiceCircuitBreaker:
    def __init__(self, *, max_consecutive_failures: int = 3):
        if type(max_consecutive_failures) is not int or max_consecutive_failures < 1:
            raise ValueError("invalid failure threshold")
        self._lock = Lock()
        self._failures = 0
        self._reason = None
        self._limit = max_consecutive_failures

    @property
    def reason(self):
        with self._lock:
            return self._reason

    def observe(self, result: dict):
        with self._lock:
            if self._reason:
                return
            if result["failure"] != "model_error":
                self._failures = 0
                return
            statuses = {e.get("http_status") for e in result["trace"] if e.get("status") == "error"}
            terminal = next((status for status in [401, 402, 403, 404] if status in statuses), None)
            if terminal:
                self._reason = f"provider_http_{terminal}"
            else:
                self._failures += 1
                if self._failures >= self._limit:
                    self._reason = "repeated_provider_failure"


def service_readiness(report: dict, *, min_tasks: int = 6, max_error_rate: float = 0.05) -> dict:
    """A successful tiny request is insufficient evidence for a full-context batch."""
    from generaldataagent.evaluation.stage_six_gates import validate_report

    if type(min_tasks) is not int or min_tasks < 1 or not 0 <= max_error_rate <= 1:
        raise ValueError("invalid service calibration policy")
    summary = validate_report(report)
    rows = report["results"]
    errors = sum(
        row["failure"] in {"model_error", "worker_error", "worker_deadline", "service_circuit_open"}
        for row in rows
    )
    replied = sum(row["tokens"] > 0 for row in rows)
    blockers = []
    if summary["n"] < min_tasks:
        blockers.append("insufficient_calibration_tasks")
    if report.get("requested_n", summary["n"]) != summary["n"]:
        blockers.append("incomplete_calibration_cohort")
    if any(not row.get("attempted", True) for row in rows) or replied < summary["n"]:
        blockers.append("insufficient_observed_model_replies")
    if errors / summary["n"] > max_error_rate:
        blockers.append("provider_error_rate_exceeded")
    if not summary["token_telemetry_complete"]:
        blockers.append("incomplete_token_telemetry")
    return {
        "ready": not blockers,
        "n": summary["n"],
        "observed_model_replies": replied,
        "service_errors": errors,
        "service_error_rate": errors / summary["n"],
        "blocked_by": blockers,
        "scope": "sampled service and telemetry preflight; not task quality qualification",
        "production_accepted": False,
    }


def not_attempted_result(reason: str) -> dict:
    return {
        "answers": {},
        "final_sql": None,
        "failure": "service_circuit_open",
        "trace": [],
        "attempted": False,
        "service_blocker": reason,
        "tokens": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "elapsed_seconds": 0.0,
        "usage_status": "complete",
        "cost_usd": 0.0,
        "observed_cost_usd": 0.0,
        "cost_status": "no_request_made",
        # An unattempted case received no hints; the evidence field stays present.
        "experience_used": [],
    }


class PacingDeadlineExceeded(TimeoutError):
    pass


class SharedRequestPacer:
    """Reserve shared request slots across workers without storing prompts or credentials."""

    def __init__(self, database, *, scope: str, interval_seconds: float):
        import math

        if not scope or not math.isfinite(interval_seconds) or not 0 < interval_seconds <= 60:
            raise ValueError("invalid request pacing")
        self.database, self.scope, self.interval = database, scope, interval_seconds
        database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS request_slots("
                "scope TEXT PRIMARY KEY,next_at REAL NOT NULL)"
            )

    def _connect(self):
        import sqlite3

        return sqlite3.connect(self.database, timeout=5)

    def reserve(self, *, now: float, max_wait_seconds: float) -> float:
        if max_wait_seconds < 0:
            raise PacingDeadlineExceeded("no request time remains")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT next_at FROM request_slots WHERE scope=?", (self.scope,)
            ).fetchone()
            slot = max(now, row[0] if row else now)
            delay = slot - now
            if delay > max_wait_seconds:
                raise PacingDeadlineExceeded("shared service pacing exceeds remaining deadline")
            db.execute(
                "INSERT INTO request_slots VALUES (?,?) ON CONFLICT(scope) "
                "DO UPDATE SET next_at=excluded.next_at",
                (self.scope, slot + self.interval),
            )
        return delay


class PacedModel:
    def __init__(self, model, pacer: SharedRequestPacer, *, max_seconds: float = 120):
        import math
        import time

        if (
            type(max_seconds) not in {int, float}
            or not math.isfinite(max_seconds)
            or max_seconds <= 0
        ):
            raise ValueError("invalid pacing time budget")
        self.model, self.pacer, self.max_seconds = model, pacer, max_seconds
        self.deadline = time.monotonic() + max_seconds - 5

    def bind(self, **kwargs):
        """Forward tool binding without bypassing pacing or extending the task deadline."""
        bound = PacedModel(self.model.bind(**kwargs), self.pacer, max_seconds=self.max_seconds)
        bound.deadline = self.deadline
        return bound

    def invoke(self, messages):
        import time

        remaining = self.deadline - time.monotonic() - 5
        if remaining <= 0:
            raise PacingDeadlineExceeded("no request time remains")
        delay = self.pacer.reserve(now=time.time(), max_wait_seconds=remaining)
        if delay:
            time.sleep(delay)
        # Reservation can block on SQLite, and sleep can resume late. Never
        # send a request after the pacing allowance expired during either wait.
        if self.deadline - time.monotonic() - 5 <= 0:
            raise PacingDeadlineExceeded("no request time remains")
        return self.model.invoke(messages)
