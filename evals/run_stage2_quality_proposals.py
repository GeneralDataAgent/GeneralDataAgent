"""Qualify conservative Stage 2 quality-rule proposal behavior."""

import json
from collections.abc import Callable
from typing import Any

from generaldataagent.contracts import (
    ColumnProfile,
    ColumnSchema,
    DataProfile,
    RelationSchema,
)
from generaldataagent.data_engineering import propose_quality_rules


def run_quality_proposal_evaluation() -> dict[str, Any]:
    checks: list[dict[str, str]] = []
    base_schema, base_profile = _base_evidence()
    proposal = propose_quality_rules(base_schema, base_profile)
    _check(checks, "not_null_is_error", lambda: _assert_rule(proposal, "not_null", "error"))
    _check(
        checks,
        "observed_unique_is_warning",
        lambda: _assert_rule(proposal, "unique", "warning"),
    )
    _check(
        checks,
        "observed_range_is_warning",
        lambda: _assert_rule(proposal, "range", "warning"),
    )
    _check(
        checks,
        "observed_range_is_exact",
        lambda: _assert_range(proposal, {"minimum": 1, "maximum": 3}),
    )
    _check(checks, "rule_ids_are_stable", lambda: _assert_stable(base_schema, base_profile))
    _check(
        checks,
        "nullable_column_has_no_not_null",
        lambda: _assert_no_nullable_not_null(proposal),
    )
    _check(checks, "non_identifier_has_no_unique", lambda: _assert_no_non_id_unique(proposal))
    _check(checks, "empty_identifier_has_no_unique", _assert_empty_identifier_not_unique)
    _check(
        checks,
        "relation_mismatch_rejected",
        lambda: _assert_rejected("relation", base_schema, base_profile),
    )
    _check(
        checks,
        "column_mismatch_rejected",
        lambda: _assert_rejected("column", base_schema, base_profile),
    )
    _check(
        checks,
        "type_mismatch_rejected",
        lambda: _assert_rejected("type", base_schema, base_profile),
    )
    _check(
        checks,
        "row_count_mismatch_rejected",
        lambda: _assert_rejected("row_count", base_schema, base_profile),
    )
    failed = [check for check in checks if check["status"] == "failed"]
    return {
        "check_count": len(checks),
        "passed": len(checks) - len(failed),
        "failed": len(failed),
        "checks": checks,
    }


def _base_evidence() -> tuple[RelationSchema, DataProfile]:
    schema = RelationSchema(
        connector_id="warehouse",
        relation="orders",
        columns=(
            ColumnSchema(name="order_id", data_type="INTEGER", nullable=False, ordinal_position=0),
            ColumnSchema(name="channel", data_type="TEXT", nullable=True, ordinal_position=1),
        ),
    )
    profile = DataProfile(
        connector_id="warehouse",
        relation="orders",
        row_count=3,
        columns=(
            ColumnProfile(
                name="order_id",
                data_type="INTEGER",
                row_count=3,
                null_count=0,
                distinct_count=3,
                minimum=1,
                maximum=3,
                mean=2,
            ),
            ColumnProfile(
                name="channel",
                data_type="TEXT",
                row_count=3,
                null_count=0,
                distinct_count=2,
                minimum="app",
                maximum="web",
            ),
        ),
    )
    return schema, profile


def _check(checks: list[dict[str, str]], check_id: str, assertion: Callable[[], None]) -> None:
    try:
        assertion()
    except Exception as exc:
        checks.append({"check_id": check_id, "status": "failed", "error": str(exc)})
    else:
        checks.append({"check_id": check_id, "status": "passed"})


def _assert_rule(proposal: Any, rule_type: str, severity: str) -> None:
    assert any(rule.rule_type == rule_type and rule.severity == severity for rule in proposal.rules)


def _assert_range(proposal: Any, expected: dict[str, int]) -> None:
    rule = next(rule for rule in proposal.rules if rule.rule_type == "range")
    assert rule.parameters == expected


def _assert_stable(schema: RelationSchema, profile: DataProfile) -> None:
    first = propose_quality_rules(schema, profile)
    second = propose_quality_rules(schema, profile)
    assert [rule.model_dump() for rule in first.rules] == [
        rule.model_dump() for rule in second.rules
    ]


def _assert_no_nullable_not_null(proposal: Any) -> None:
    assert not any(
        rule.column == "channel" and rule.rule_type == "not_null" for rule in proposal.rules
    )


def _assert_no_non_id_unique(proposal: Any) -> None:
    assert not any(
        rule.column == "channel" and rule.rule_type == "unique" for rule in proposal.rules
    )


def _assert_empty_identifier_not_unique() -> None:
    schema, profile = _base_evidence()
    empty_profile = profile.model_copy(
        update={
            "row_count": 0,
            "columns": tuple(
                column.model_copy(
                    update={
                        "row_count": 0,
                        "null_count": 0,
                        "distinct_count": 0,
                        "minimum": None,
                        "maximum": None,
                        "mean": None,
                    }
                )
                for column in profile.columns
            ),
        }
    )
    proposal = propose_quality_rules(schema, empty_profile)
    assert not any(rule.rule_type == "unique" for rule in proposal.rules)


def _assert_rejected(kind: str, schema: RelationSchema, profile: DataProfile) -> None:
    if kind == "relation":
        candidate = profile.model_copy(update={"relation": "customers"})
    elif kind == "column":
        candidate = profile.model_copy(update={"columns": profile.columns[:1]})
    elif kind == "type":
        changed = profile.columns[0].model_copy(update={"data_type": "TEXT"})
        candidate = profile.model_copy(update={"columns": (changed, *profile.columns[1:])})
    else:
        changed = profile.columns[0].model_copy(update={"row_count": 2})
        candidate = profile.model_copy(update={"columns": (changed, *profile.columns[1:])})
    try:
        propose_quality_rules(schema, candidate)
    except ValueError:
        return
    raise AssertionError(f"{kind} mismatch was accepted")


if __name__ == "__main__":
    result = run_quality_proposal_evaluation()
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0 if result["failed"] == 0 else 1)
