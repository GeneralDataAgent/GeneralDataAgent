"""Run the frozen deterministic Stage 2 engineering baseline."""

import json
import sqlite3
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from generaldataagent.connectors import SQLiteConnector
from generaldataagent.contracts import (
    ColumnSchema,
    LineageAsset,
    QualityRule,
    RelationSchema,
    TransformationTest,
)
from generaldataagent.data_engineering import (
    analyze_impact,
    build_transformation_proposal,
    compare_schemas,
    execute_quality_rules,
    profile_relation,
)

DATASET = Path(__file__).parent / "datasets" / "stage2.jsonl"


def run_evaluation(dataset_path: Path = DATASET) -> dict[str, Any]:
    cases = [json.loads(line) for line in dataset_path.read_text().splitlines() if line.strip()]
    failures: list[dict[str, str]] = []
    counts = Counter(case["category"] for case in cases)
    with TemporaryDirectory(prefix="generaldataagent-stage2-eval-") as directory:
        database = Path(directory) / "quality.sqlite"
        _create_quality_fixture(database)
        connector = SQLiteConnector(database, connector_id="evaluation")
        for case in cases:
            try:
                _evaluate_case(case, connector)
            except Exception as exc:
                failures.append(
                    {
                        "case_id": case["case_id"],
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
    passed = len(cases) - len(failures)
    return {
        "dataset_size": len(cases),
        "category_counts": dict(sorted(counts.items())),
        "passed": passed,
        "failed": len(failures),
        "pass_rate": passed / len(cases) if cases else 0.0,
        "failures": failures,
    }


def _evaluate_case(case: dict[str, Any], connector: SQLiteConnector) -> None:
    category = case["category"]
    if category == "schema_diff":
        _evaluate_schema_diff(case)
    elif category == "quality":
        _evaluate_quality(case, connector)
    elif category == "impact":
        _evaluate_impact(case)
    elif category == "transformation":
        _evaluate_transformation(case)
    elif category == "profile_oracle":
        _evaluate_profile_oracle(case, connector)
    else:
        raise ValueError(f"unsupported Stage 2 category: {category}")


def _evaluate_schema_diff(case: dict[str, Any]) -> None:
    before = _relation_schema(case["before"])
    after = _relation_schema(case["after"])
    result = compare_schemas(before, after)
    actual = [[item.name, item.change_type] for item in result.changes]
    assert result.breaking == case["expected"]["breaking"]
    assert actual == case["expected"]["changes"]


def _relation_schema(columns: list[list[Any]]) -> RelationSchema:
    return RelationSchema(
        connector_id="evaluation",
        relation="orders",
        columns=tuple(
            ColumnSchema(
                name=name,
                data_type=data_type,
                nullable=nullable,
                ordinal_position=index,
            )
            for index, (name, data_type, nullable) in enumerate(columns)
        ),
    )


def _evaluate_quality(case: dict[str, Any], connector: SQLiteConnector) -> None:
    result = execute_quality_rules(
        connector,
        "orders",
        (QualityRule.model_validate(case["rule"]),),
    )
    assert result.checks[0].violation_count == case["expected"]["violations"]
    assert result.passed == case["expected"]["passed"]


def _evaluate_impact(case: dict[str, Any]) -> None:
    result = analyze_impact(
        tuple(
            LineageAsset(asset_id=asset_id, asset_type=asset_type, depends_on=tuple(depends_on))
            for asset_id, asset_type, depends_on in case["assets"]
        ),
        tuple(case["changed"]),
    )
    actual = [[item.asset_id, item.distance] for item in result.impacted_assets]
    assert actual == case["expected"]["assets"]
    assert result.has_cycles == case["expected"]["cycles"]


def _evaluate_transformation(case: dict[str, Any]) -> None:
    expected_status = case["expected"]["status"]
    try:
        result = build_transformation_proposal(
            connector_id="evaluation",
            dialect="sqlite",
            target_relation="proposed_orders",
            select_sql=case["sql"],
            allowed_relations=set(case["allowed"]),
            tests=(TransformationTest(name="has-rows", assertion="row_count_nonzero"),),
            impact_summary="Frozen Stage 2 evaluation case.",
            rollback_plan="Discard the dry-run proposal.",
        )
    except ValueError:
        if expected_status != "rejected":
            raise
        return
    assert expected_status == "accepted"
    assert list(result.dependencies) == case["expected"]["dependencies"]


def _evaluate_profile_oracle(case: dict[str, Any], connector: SQLiteConnector) -> None:
    result = profile_relation(connector, "orders")
    expected = case["expected"]
    if expected["column"] is None:
        actual = getattr(result, expected["metric"])
    else:
        column = next(item for item in result.columns if item.name == expected["column"])
        actual = getattr(column, expected["metric"])
    assert actual == expected["value"]


def _create_quality_fixture(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE orders (id INTEGER NOT NULL, channel TEXT, amount REAL)"
        )
        connection.executemany(
            "INSERT INTO orders VALUES (?, ?, ?)",
            [(1, "app", 10.0), (2, "web", 20.0), (3, None, 30.0)],
        )


if __name__ == "__main__":
    print(json.dumps(run_evaluation(), indent=2, sort_keys=True))
