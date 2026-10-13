"""Parsed SQL read-only enforcement."""

from dataclasses import dataclass

from sqlglot import exp, parse
from sqlglot.errors import ParseError


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

        if any(isinstance(node, exp.Lock) for node in statement.walk()):
            raise SqlGuardError("locking queries are not allowed")

        cte_aliases = {
            cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE) if cte.alias_or_name
        }
        relations = frozenset(
            name
            for table in statement.find_all(exp.Table)
            if (name := self._relation_name(table)) and name.lower() not in cte_aliases
        )
        if allowed_relations is not None:
            normalized_allowed = {name.lower() for name in allowed_relations}
            unauthorized = {name for name in relations if name.lower() not in normalized_allowed}
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
