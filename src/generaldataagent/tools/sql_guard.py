"""Parsed SQL read-only enforcement."""

import logging
from dataclasses import dataclass

from sqlglot import exp, parse
from sqlglot.errors import ParseError
from sqlglot.optimizer.scope import traverse_scope


class _SQLDiagnosticFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Parser fallback diagnostics can otherwise contain raw SQL literals/credentials.
        record.msg, record.args = "SQL parser diagnostic suppressed", ()
        return True


logging.getLogger("sqlglot").addFilter(_SQLDiagnosticFilter())


class SqlGuardError(ValueError):
    """Raised when SQL violates the read-only policy."""


@dataclass(frozen=True, slots=True)
class SqlInspection:
    normalized_sql: str
    relations: frozenset[str]


class SqlGuard:
    """Rejects mutation, multi-statement, locking, and unauthorized relations."""

    _MUTATIONS = (
        exp.Insert,
        exp.Update,
        exp.Delete,
        exp.Create,
        exp.Drop,
        exp.Alter,
        exp.Merge,
        exp.Command,
    )

    def __init__(self, *, dialect: str | None = None) -> None:
        self.dialect = dialect

    def inspect(self, sql: str, *, allowed_relations: set[str] | None = None) -> SqlInspection:
        if not sql.strip():
            raise SqlGuardError("SQL must not be empty")
        try:
            statements = [statement for statement in parse(sql, read=self.dialect) if statement]
        except ParseError as exc:
            raise SqlGuardError(f"SQL parsing failed: {exc}") from exc

        if len(statements) != 1:
            raise SqlGuardError("exactly one SQL statement is allowed")

        statement = statements[0]
        if not isinstance(statement, (exp.Query, exp.Select, exp.Union)):
            raise SqlGuardError(f"only read-only query statements are allowed, got {statement.key}")

        mutation = next(
            (node for node in statement.walk() if isinstance(node, self._MUTATIONS)),
            None,
        )
        if mutation is not None:
            raise SqlGuardError(f"mutation node {mutation.key!r} is not allowed")

        if any(isinstance(node, exp.Into) for node in statement.walk()):
            raise SqlGuardError("SELECT INTO is not read-only")
        for function in statement.find_all(exp.Func):
            if isinstance(function, exp.Anonymous):
                raise SqlGuardError("unreviewed SQL functions are not allowed")
            if function.sql_name().upper() in {
                "READ_CSV",
                "READ_CSV_AUTO",
                "READ_JSON",
                "READ_PARQUET",
                "NEXT_VALUE_FOR",
            }:
                raise SqlGuardError("external I/O and stateful functions are not allowed")
        if any(
            not isinstance(table.this, exp.Identifier) for table in statement.find_all(exp.Table)
        ):
            raise SqlGuardError("table-valued functions are not allowed")

        if any(isinstance(node, exp.Lock) for node in statement.walk()):
            raise SqlGuardError("locking queries are not allowed")

        # Resolve CTEs lexically; a name in a nested CTE must not whitelist a physical table.
        relations = frozenset(
            self._relation_name(source)
            for scope in traverse_scope(statement)
            for source in scope.sources.values()
            if isinstance(source, exp.Table)
        )
        if allowed_relations is not None:
            # SQLite folds ASCII only: distinct quoted Unicode table names must never
            # gain one another's authorization through Python's Unicode lowercasing.
            def relation_key(name):
                if self.dialect == "sqlite":
                    return name.translate(
                        str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
                    )
                return name.lower()

            normalized_allowed = {relation_key(name) for name in allowed_relations}
            unauthorized = {
                name for name in relations if relation_key(name) not in normalized_allowed
            }
            if unauthorized:
                raise SqlGuardError(f"unauthorized relations: {sorted(unauthorized)}")

        return SqlInspection(
            normalized_sql=statement.sql(dialect=self.dialect, pretty=False),
            relations=relations,
        )

    @staticmethod
    def _relation_name(table: exp.Table) -> str:
        parts = [part for part in (table.catalog, table.db, table.name) if part]
        return ".".join(parts)
