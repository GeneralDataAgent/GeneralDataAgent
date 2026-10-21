"""Read-only SQLite execution adapter for the first vertical slice."""

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class QueryExecutionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class QueryResult:
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    elapsed_ms: float
    query_plan: tuple[tuple[Any, ...], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "columns": list(self.columns),
            "rows": [list(row) for row in self.rows],
            "row_count": len(self.rows),
            "elapsed_ms": round(self.elapsed_ms, 3),
            "query_plan": [list(row) for row in self.query_plan],
        }


class SQLiteReadOnlyExecutor:
    """Opens a fixed database in SQLite read-only mode and enforces hard limits."""

    def __init__(
        self,
        database_path: Path,
        *,
        max_rows: int = 1_000,
        max_cells: int | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        if max_rows < 1:
            raise ValueError("max_rows must be positive")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_cells is not None and (type(max_cells) is not int or max_cells < 1):
            raise ValueError("max_cells must be a positive integer")
        self.database_path = database_path.resolve(strict=True)
        self.max_rows = max_rows
        self.max_cells = max_cells
        self.timeout_seconds = timeout_seconds

    def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> QueryResult:
        started = time.monotonic()
        deadline = started + self.timeout_seconds
        uri = f"file:{self.database_path.as_posix()}?mode=ro"

        try:
            with sqlite3.connect(uri, uri=True, timeout=self.timeout_seconds) as connection:
                connection.execute("PRAGMA query_only = ON")
                connection.set_progress_handler(
                    lambda: 1 if time.monotonic() > deadline else 0,
                    1_000,
                )
                plan = tuple(connection.execute(f"EXPLAIN QUERY PLAN {sql}", parameters).fetchall())
                cursor = connection.execute(sql, parameters)
                columns = tuple(item[0] for item in (cursor.description or ()))
                row_limit = self.max_rows
                if self.max_cells is not None and columns:
                    row_limit = min(row_limit, self.max_cells // len(columns))
                    if row_limit < 1:
                        raise QueryExecutionError("query output width exceeded the cell limit")
                rows = tuple(cursor.fetchmany(row_limit + 1))
        except sqlite3.Error as exc:
            raise QueryExecutionError(f"read-only query failed: {exc}") from exc

        if self.max_cells is not None and len(rows) * len(columns) > self.max_cells:
            raise QueryExecutionError(f"query exceeded the {self.max_cells} cell limit")
        if len(rows) > self.max_rows:
            raise QueryExecutionError(f"query exceeded the {self.max_rows} row limit")
        return QueryResult(
            columns=columns,
            rows=rows,
            elapsed_ms=(time.monotonic() - started) * 1_000,
            query_plan=plan,
        )
