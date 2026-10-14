"""Read-only SQLite connector used by deterministic engineering tools."""

import sqlite3
import time
from pathlib import Path
from typing import Any

from generaldataagent.connectors.base import (
    ConnectorError,
    ConnectorErrorCode,
    quote_identifier,
)
from generaldataagent.contracts.data_engineering import (
    ColumnSchema,
    ConnectorCapabilities,
    RelationSchema,
)


class SQLiteConnector:
    def __init__(
        self,
        database_path: Path,
        *,
        connector_id: str = "sqlite",
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
            dialect="sqlite",
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

    def inspect_relation(self, relation: str) -> RelationSchema:
        rows = self.fetch_all(f"PRAGMA table_info({quote_identifier(relation)})")
        if not rows:
            raise ConnectorError(
                ConnectorErrorCode.NOT_FOUND,
                f"relation not found: {relation}",
            )
        columns = tuple(
            ColumnSchema(
                name=str(row[1]),
                data_type=str(row[2] or "UNKNOWN").upper(),
                nullable=not bool(row[3]),
                ordinal_position=int(row[0]),
            )
            for row in rows
        )
        return RelationSchema(
            connector_id=self.capabilities.connector_id,
            relation=relation,
            columns=columns,
        )

    def fetch_all(
        self,
        sql: str,
        parameters: tuple[Any, ...] = (),
    ) -> tuple[tuple[Any, ...], ...]:
        uri = f"file:{self.database_path.as_posix()}?mode=ro"
        deadline = time.monotonic() + self.timeout_seconds
        try:
            with sqlite3.connect(uri, uri=True) as connection:
                connection.execute("PRAGMA query_only = ON")
                connection.set_progress_handler(
                    lambda: 1 if time.monotonic() > deadline else 0,
                    1_000,
                )
                return tuple(connection.execute(sql, parameters).fetchall())
        except sqlite3.OperationalError as exc:
            message = str(exc)
            lowered = message.casefold()
            if "interrupted" in lowered:
                raise ConnectorError(
                    ConnectorErrorCode.TIMEOUT,
                    f"SQLite request exceeded {self.timeout_seconds:g} seconds",
                    retryable=True,
                ) from exc
            code = ConnectorErrorCode.EXECUTION_FAILED
            if any(marker in lowered for marker in ("read-only", "readonly")):
                code = ConnectorErrorCode.PERMISSION_DENIED
            raise ConnectorError(code, f"SQLite request failed: {message}") from exc
        except sqlite3.Error as exc:
            raise ConnectorError(
                ConnectorErrorCode.EXECUTION_FAILED,
                f"SQLite request failed: {exc}",
            ) from exc
