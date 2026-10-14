"""Run the frozen Stage 2 schema-diff matrix."""

import json
from pathlib import Path
from typing import Any

from generaldataagent.contracts import ColumnSchema, RelationSchema
from generaldataagent.data_engineering import compare_schemas

DATASET = Path(__file__).parent / "datasets" / "stage2-schema.jsonl"


def run_schema_evaluation(dataset_path: Path = DATASET) -> dict[str, Any]:
    cases = [json.loads(line) for line in dataset_path.read_text().splitlines() if line.strip()]
    failures: list[dict[str, str]] = []
    for case in cases:
        try:
            before = _schema(case["before"], case.get("before_connector", "before"))
            after = _schema(case["after"], case.get("after_connector", "after"))
            result = compare_schemas(before, after)
            actual = [[item.name, item.change_type] for item in result.changes]
            assert actual == case["expected"]["changes"]
            assert result.breaking == case["expected"]["breaking"]
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
        "passed": passed,
        "failed": len(failures),
        "pass_rate": passed / len(cases) if cases else 0.0,
        "failures": failures,
    }


def _schema(columns: list[list[Any]], connector_id: str) -> RelationSchema:
    return RelationSchema(
        connector_id=connector_id,
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


if __name__ == "__main__":
    result = run_schema_evaluation()
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0 if result["failed"] == 0 else 1)
