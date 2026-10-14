"""Read-only DuckDB connector providing the second Stage 2 data source."""

from pathlib import Path
from threading import Event, Thread, Timer
from typing import Any

import duckdb

from generaldataagent.connectors.base import ConnectorError, ConnectorErrorCode
from generaldataagent.contracts.data_engineering import (
    ColumnSchema,
    ConnectorCapabilities,
    RelationSchema,
)


class DuckDBConnector:
    def __init__(
        self,
        database_path: Path,
        *,
        connector_id: str = "duckdb",
        timeout_seconds: float = 30.0,
    ) -> None:
        try:
            self.database_path = database_path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ConnectorError(ConnectorErrorCode.NOT_FOUND, str(exc)) from exc
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = timeout_seconds
        self._capabilities = ConnectorCapabilities(
            connector_id=connector_id,
            dialect="duckdb",
            read_only=True,
            schema_introspection=True,
            profiling=True,
            sampling=True,
            explain=True,
            cancellation=True,
        )

    @property
    def capabilities(self) -> ConnectorCapabilities:
        return self._capabilities

    def inspect_relation(
        self,
        relation: str,
        *,
        cancel_event: Event | None = None,
    ) -> RelationSchema:
        rows = self.fetch_all(
            """
            SELECT column_name, data_type, is_nullable, ordinal_position
            FROM information_schema.columns
            WHERE table_catalog = current_database()
              AND table_schema = current_schema()
              AND table_name = ?
            ORDER BY ordinal_position
            """,
            (relation,),
            cancel_event=cancel_event,
        )
        if not rows:
            raise ConnectorError(
                ConnectorErrorCode.NOT_FOUND,
                f"relation not found: {relation}",
            )
        return RelationSchema(
            connector_id=self.capabilities.connector_id,
            relation=relation,
            columns=tuple(
                ColumnSchema(
                    name=str(row[0]),
                    data_type=str(row[1]).upper(),
                    nullable=str(row[2]).casefold() == "yes",
                    ordinal_position=int(row[3]) - 1,
                )
                for row in rows
            ),
        )

    def fetch_all(
        self,
        sql: str,
        parameters: tuple[Any, ...] = (),
        *,
        cancel_event: Event | None = None,
    ) -> tuple[tuple[Any, ...], ...]:
        try:
            with duckdb.connect(str(self.database_path), read_only=True) as connection:
                timed_out = Event()
                finished = Event()

                def interrupt_for_timeout() -> None:
                    timed_out.set()
                    connection.interrupt()

                def interrupt_for_cancellation() -> None:
                    while not finished.wait(0.01):
                        if cancel_event is not None and cancel_event.is_set():
                            connection.interrupt()
                            return

                timer = Timer(self.timeout_seconds, interrupt_for_timeout)
                timer.daemon = True
                timer.start()
                watcher = None
                if cancel_event is not None:
                    watcher = Thread(target=interrupt_for_cancellation, daemon=True)
                    watcher.start()
                try:
                    return tuple(connection.execute(sql, parameters).fetchall())
                finally:
                    finished.set()
                    timer.cancel()
                    if watcher is not None:
                        watcher.join(timeout=0.1)
        except duckdb.Error as exc:
            message = str(exc)
            lowered = message.casefold()
            if "interrupt" in lowered:
                if cancel_event is not None and cancel_event.is_set() and not timed_out.is_set():
                    raise ConnectorError(
                        ConnectorErrorCode.CANCELLED,
                        "DuckDB request was cancelled",
                    ) from exc
                raise ConnectorError(
                    ConnectorErrorCode.TIMEOUT,
                    f"DuckDB request exceeded {self.timeout_seconds:g} seconds",
                    retryable=True,
                ) from exc
            code = ConnectorErrorCode.PERMISSION_DENIED
            if "read-only" not in lowered:
                code = ConnectorErrorCode.EXECUTION_FAILED
            raise ConnectorError(code, f"DuckDB request failed: {message}") from exc
