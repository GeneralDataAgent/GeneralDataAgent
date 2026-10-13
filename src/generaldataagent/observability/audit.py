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
