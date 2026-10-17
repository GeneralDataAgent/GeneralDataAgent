"""Bounded aggregate SLO measurements without request/user payload retention."""

import math
from collections import deque
from threading import Lock


class ServiceSLO:
    def __init__(self, *, capacity: int = 10000) -> None:
        if not 100 <= capacity <= 100000:
            raise ValueError("SLO window capacity must be between 100 and 100000")
        self._events: deque[tuple[float, bool]] = deque(maxlen=capacity)
        self._lock = Lock()

    def record(self, *, duration_seconds: float, available: bool) -> None:
        if (
            not math.isfinite(duration_seconds)
            or duration_seconds < 0
            or type(available) is not bool
        ):
            raise ValueError("invalid SLO sample")
        with self._lock:
            self._events.append((duration_seconds, available))

    def snapshot(self) -> dict[str, int | float | None]:
        with self._lock:
            events = tuple(self._events)
        latency = sorted(duration for duration, _ in events)
        total = len(events)
        return {
            "sample_count": total,
            "available_count": sum(available for _, available in events),
            "p95_seconds": latency[math.ceil(total * 0.95) - 1] if total else None,
        }
