"""Deterministic execution of a constrained data-quality rule set."""

from collections.abc import Iterable
from typing import Any

from generaldataagent.connectors.base import ReadOnlyConnector, quote_identifier
from generaldataagent.contracts.data_engineering import (
    QualityCheckResult,
    QualityReport,
    QualityRule,
)


def execute_quality_rules(
    connector: ReadOnlyConnector,
    relation: str,
    rules: Iterable[QualityRule],
) -> QualityReport:
    if not connector.capabilities.read_only or not connector.capabilities.profiling:
        raise ValueError("connector must declare read-only profiling support")

    schema = connector.inspect_relation(relation)
    known_columns = {column.name.casefold(): column.name for column in schema.columns}
    relation_sql = quote_identifier(relation)
    row_count = int(connector.fetch_all(f"SELECT COUNT(*) FROM {relation_sql}")[0][0])
    checks: list[QualityCheckResult] = []
    seen_rule_ids: set[str] = set()
    for rule in rules:
        if rule.rule_id in seen_rule_ids:
            raise ValueError(f"duplicate quality rule id: {rule.rule_id}")
        seen_rule_ids.add(rule.rule_id)
        column_name = known_columns.get(rule.column.casefold())
        if column_name is None:
            raise ValueError(f"quality rule references an unknown column: {rule.column}")
        column_sql = quote_identifier(column_name)
        sql, parameters = _violation_query(rule, column_sql, relation_sql)
        violation_count = int(connector.fetch_all(sql, parameters)[0][0])
        checks.append(
            QualityCheckResult(
                rule_id=rule.rule_id,
                column=column_name,
                rule_type=rule.rule_type,
                severity=rule.severity,
                passed=violation_count == 0,
                violation_count=violation_count,
            )
        )
    return QualityReport(
        connector_id=connector.capabilities.connector_id,
        relation=relation,
        row_count=row_count,
        checks=tuple(checks),
        passed=not any(not check.passed and check.severity == "error" for check in checks),
    )


def _violation_query(
    rule: QualityRule,
    column_sql: str,
    relation_sql: str,
) -> tuple[str, tuple[Any, ...]]:
    if rule.rule_type == "not_null":
        return f"SELECT COUNT(*) FROM {relation_sql} WHERE {column_sql} IS NULL", ()
    if rule.rule_type == "unique":
        return (
            f"SELECT COUNT(*) - COUNT(DISTINCT {column_sql}) FROM {relation_sql} "
            f"WHERE {column_sql} IS NOT NULL",
            (),
        )
    if rule.rule_type == "accepted_values":
        values = tuple(rule.parameters["values"])
        placeholders = ", ".join("?" for _ in values)
        return (
            f"SELECT COUNT(*) FROM {relation_sql} WHERE {column_sql} IS NOT NULL "
            f"AND {column_sql} NOT IN ({placeholders})",
            values,
        )

    clauses: list[str] = []
    parameters: list[Any] = []
    if "minimum" in rule.parameters:
        clauses.append(f"{column_sql} < ?")
        parameters.append(rule.parameters["minimum"])
    if "maximum" in rule.parameters:
        clauses.append(f"{column_sql} > ?")
        parameters.append(rule.parameters["maximum"])
    predicate = " OR ".join(clauses)
    return (
        f"SELECT COUNT(*) FROM {relation_sql} WHERE {column_sql} IS NOT NULL "
        f"AND ({predicate})",
        tuple(parameters),
    )
