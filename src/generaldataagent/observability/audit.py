"""Append-only development audit sink."""

from pathlib import Path

from generaldataagent.contracts import AuditEvent


class JsonlAuditSink:
    """Writes one canonical JSON audit event per line.

    This sink is for local development. Production must use an independently
    authorized append-only service.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: AuditEvent) -> None:
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(event.model_dump_json() + "\n")


class MinimalAuditSink(JsonlAuditSink):
    """Pilot sink that suppresses free-form payloads before persistence.

    Only numeric measurements and boolean flags under fixed keys survive. Neither
    SQL, prompts, results, errors nor credentials are copied into the audit log.
    Identity envelope fields must contain opaque service-issued identifiers.
    This remains local storage, not an independently authorized production sink.
    """

    _COUNTERS = frozenset({"row_count", "duration_ms", "size_bytes", "cost_microunits"})
    _FLAGS = frozenset({"allowed", "approved", "success"})

    def emit(self, event: AuditEvent) -> None:
        payload = {}
        for key, value in event.payload.items():
            if (
                key in self._COUNTERS
                and type(value) is int
                and value >= 0
                or key in self._FLAGS
                and type(value) is bool
            ):
                payload[key] = value
        super().emit(event.model_copy(update={"payload": payload}))
