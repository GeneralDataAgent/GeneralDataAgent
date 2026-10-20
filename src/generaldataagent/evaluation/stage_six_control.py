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
    }
