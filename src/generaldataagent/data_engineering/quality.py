"""Deterministic execution of a constrained data-quality rule set."""

import re
from collections.abc import Iterable
from typing import Any

from generaldataagent.connectors.base import ReadOnlyConnector, quote_identifier
from generaldataagent.contracts.data_engineering import (
    DataProfile,
    QualityCheckResult,
    QualityReport,
    QualityRule,
    QualityRuleProposal,
    RelationSchema,
)

_NUMERIC_TYPE_MARKERS = ("INT", "REAL", "FLOAT", "DOUBLE", "DECIMAL", "NUMERIC")


def propose_quality_rules(
    schema: RelationSchema,
    profile: DataProfile,
) -> QualityRuleProposal:
    """Build conservative rules from one matching schema/profile snapshot."""
    if schema.connector_id != profile.connector_id or schema.relation != profile.relation:
        raise ValueError("quality proposal schema and profile must describe the same relation")
    schema_columns = {column.name.casefold(): column for column in schema.columns}
    profile_columns = {column.name.casefold(): column for column in profile.columns}
    if schema_columns.keys() != profile_columns.keys():
        raise ValueError("quality proposal schema and profile columns must match")

    rules: list[QualityRule] = []
    for ordinal, column in enumerate(schema.columns):
        observed = profile_columns[column.name.casefold()]
        if column.data_type.casefold() != observed.data_type.casefold():
            raise ValueError(f"quality proposal data type mismatch: {column.name}")
        if observed.row_count != profile.row_count:
            raise ValueError(f"quality proposal row count mismatch: {column.name}")
        if not column.nullable:
            rules.append(
                QualityRule(
                    rule_id=_rule_id("not_null", ordinal, column.name),
                    column=column.name,
                    rule_type="not_null",
                    severity="error",
                )
            )
        id_like = column.name.casefold() == "id" or column.name.casefold().endswith("_id")
        if (
            id_like
            and profile.row_count > 0
            and observed.null_count == 0
            and observed.distinct_count == profile.row_count
        ):
            rules.append(
                QualityRule(
                    rule_id=_rule_id("unique", ordinal, column.name),
                    column=column.name,
                    rule_type="unique",
                    severity="warning",
                )
            )
        numeric = any(marker in column.data_type.upper() for marker in _NUMERIC_TYPE_MARKERS)
        if numeric and observed.minimum is not None and observed.maximum is not None:
            rules.append(
                QualityRule(
                    rule_id=_rule_id("observed_range", ordinal, column.name),
                    column=column.name,
                    rule_type="range",
                    parameters={"minimum": observed.minimum, "maximum": observed.maximum},
                    severity="warning",
                )
            )
    if not rules:
        raise ValueError("quality proposal found no evidence-backed rule candidates")
    return QualityRuleProposal(
        connector_id=schema.connector_id,
        relation=schema.relation,
        rules=tuple(rules),
        notes=(
            "Non-nullable schema columns produce error-severity not-null rules.",
            "Observed identifier uniqueness and numeric ranges are warning-only snapshot evidence.",
        ),
    )


def _rule_id(prefix: str, ordinal: int, column: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", column.casefold()).strip("_") or "column"
    return f"{prefix}_{ordinal:03d}_{slug}"[:64].rstrip("_")


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
