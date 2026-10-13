"""Read-only DuckDB connector providing the second Stage 2 data source."""

from pathlib import Path
from typing import Any

import duckdb

from generaldataagent.connectors.base import ConnectorError, ConnectorErrorCode
from generaldataagent.contracts.data_engineering import (
    ColumnSchema,
    ConnectorCapabilities,
    RelationSchema,
)


class DuckDBConnector:
    def __init__(self, database_path: Path, *, connector_id: str = "duckdb") -> None:
        try:
            self.database_path = database_path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ConnectorError(ConnectorErrorCode.NOT_FOUND, str(exc)) from exc
        self._capabilities = ConnectorCapabilities(
            connector_id=connector_id,
            dialect="duckdb",
            read_only=True,
            schema_introspection=True,
            profiling=True,
            sampling=True,
            explain=True,
        )

    @property
    def capabilities(self) -> ConnectorCapabilities:
        return self._capabilities

    def inspect_relation(self, relation: str) -> RelationSchema:
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
    ) -> tuple[tuple[Any, ...], ...]:
        try:
            with duckdb.connect(str(self.database_path), read_only=True) as connection:
                return tuple(connection.execute(sql, parameters).fetchall())
        except duckdb.Error as exc:
            message = str(exc)
            code = (
                ConnectorErrorCode.PERMISSION_DENIED
                if "read-only" in message.casefold()
                else ConnectorErrorCode.EXECUTION_FAILED
            )
            raise ConnectorError(code, f"DuckDB request failed: {message}") from exc
