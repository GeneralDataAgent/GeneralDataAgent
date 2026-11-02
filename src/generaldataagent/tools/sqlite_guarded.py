"""Opt-in cancellable SQLite adapter; the frozen benchmark executor stays unchanged."""

import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from contextvars import ContextVar
from typing import Any

from generaldataagent.tools.sqlite import QueryExecutionError, QueryResult, SQLiteReadOnlyExecutor


class GuardedSQLiteReadOnlyExecutor(SQLiteReadOnlyExecutor):
    """Local task adapter with VM guard checks and explicit connection cleanup.

    Deliberately separate from the legacy executor: frozen evaluation protocols hash
    that class's source. Do not silently alter historical benchmark execution semantics.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._execution_guard: ContextVar[Callable[[], None] | None] = ContextVar(
            "sqlite_execution_guard", default=None
        )

    @contextmanager
    def guarded(self, guard: Callable[[], None]) -> Iterator[None]:
        """Bind a request-local guard; concurrent/nested executions do not share it."""
        token = self._execution_guard.set(guard)
        try:
            guard()
            yield
            guard()
        finally:
            self._execution_guard.reset(token)

    def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> QueryResult:
        started = time.monotonic()
        deadline = started + self.timeout_seconds
        uri = f"file:{self.database_path.as_posix()}?mode=ro"

        guard = self._execution_guard.get()
        guard_error: Exception | None = None
        next_check = started

        def progress():
            nonlocal guard_error, next_check
            now = time.monotonic()
            if guard is not None and now >= next_check:
                try:
                    guard()
                except Exception as exc:
                    guard_error = exc
                    return 1
                next_check = now + 0.05
            return int(now > deadline)

        if guard is not None:
            guard()
        try:
            # sqlite3.Connection's context manager commits/rolls back but does NOT close.
            # Close the actual connection before acknowledging an execution stop.
            with closing(
                sqlite3.connect(uri, uri=True, timeout=self.timeout_seconds)
            ) as connection:
                connection.execute("PRAGMA query_only = ON")
                connection.set_progress_handler(progress, 1_000)
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
            if guard_error is not None:
                raise guard_error from exc
            raise QueryExecutionError(f"read-only query failed: {exc}") from exc

        if guard is not None:
            guard()
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
