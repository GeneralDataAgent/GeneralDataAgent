"""Run the frozen Stage 2 data-quality rule matrix."""

import json
import sqlite3
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from generaldataagent.connectors import SQLiteConnector
from generaldataagent.contracts import QualityRule
from generaldataagent.data_engineering import execute_quality_rules

DATASET = Path(__file__).parent / "datasets" / "stage2-quality.jsonl"


def run_quality_evaluation(dataset_path: Path = DATASET) -> dict[str, Any]:
    cases = [json.loads(line) for line in dataset_path.read_text().splitlines() if line.strip()]
    failures: list[dict[str, Any]] = []
    counts = Counter(case["rule"]["rule_type"] for case in cases)
    with TemporaryDirectory(prefix="generaldataagent-quality-eval-") as directory:
        database = Path(directory) / "quality.sqlite"
        _create_fixture(database)
        connector = SQLiteConnector(database, connector_id="quality-evaluation")
        for case in cases:
            try:
                report = execute_quality_rules(
                    connector,
                    "orders",
                    (QualityRule.model_validate(case["rule"]),),
                )
                check = report.checks[0]
                expected = case["expected"]
                assert check.violation_count == expected["violations"]
                assert report.passed == expected["passed"]
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
        "rule_type_counts": dict(sorted(counts.items())),
        "passed": passed,
        "failed": len(failures),
        "pass_rate": passed / len(cases) if cases else 0.0,
        "failures": failures,
    }


def _create_fixture(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE orders (id INTEGER, channel TEXT, amount REAL, status TEXT)"
        )
        connection.executemany(
            "INSERT INTO orders VALUES (?, ?, ?, ?)",
            [
                (1, "app", 10.0, "paid"),
                (2, "web", 20.0, "paid"),
                (2, "app", -5.0, "refunded"),
                (4, "store", 100.0, "pending"),
                (None, None, None, "paid"),
                (6, "partner", 50.0, None),
                (7, "web", 0.0, "cancelled"),
                (8, "app", 75.0, "paid"),
                (9, "store", 25.0, "pending"),
                (10, "app", 200.0, "unknown"),
            ],
        )


if __name__ == "__main__":
    result = run_quality_evaluation()
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0 if result["failed"] == 0 else 1)
