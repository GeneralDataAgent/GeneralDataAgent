"""Execute the frozen Stage 1 dataset against the deterministic TLC runtime."""

import json
from pathlib import Path

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.datasets import (
    build_tlc_metric_catalog,
    download_tlc_sources,
    prepare_tlc_database,
)
from generaldataagent.evaluation import (
    evaluate_semantic_resolution,
    evaluate_stage_one,
    load_semantic_cases,
    load_stage_one_cases,
)
from generaldataagent.orchestration import MetricAnalysisRuntime
from generaldataagent.tools import SQLiteReadOnlyExecutor


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    trips, zones = download_tlc_sources(root / "data" / "tlc" / "raw")
    database = root / "data" / "tlc" / "tlc-2025-01.sqlite"
    prepare_tlc_database(trips, zones, database)
    runtime = MetricAnalysisRuntime(
        catalog=build_tlc_metric_catalog(),
        executor=SQLiteReadOnlyExecutor(database, max_rows=100),
        artifacts=LocalArtifactStore(root / ".artifacts" / "stage1-evaluation"),
    )
    cases = load_stage_one_cases(root / "evals" / "datasets" / "stage1.jsonl")
    report = evaluate_stage_one(runtime, cases)
    failures = [item.model_dump(mode="json") for item in report.results if not item.passed]
    semantic_cases = load_semantic_cases(root / "evals" / "datasets" / "stage1-semantic.jsonl")
    semantic_report = evaluate_semantic_resolution(runtime, semantic_cases)
    semantic_failures = [
        item.model_dump(mode="json") for item in semantic_report.results if not item.passed
    ]
    print(
        json.dumps(
            {
                "metric_execution": {
                    "dataset_size": report.dataset_size,
                    "passed": report.passed,
                    "failed": report.failed,
                    "pass_rate": report.pass_rate,
                    "category_counts": report.category_counts,
                    "failures": failures,
                },
                "semantic_resolution": {
                    "dataset_size": semantic_report.dataset_size,
                    "passed": semantic_report.passed,
                    "failed": semantic_report.failed,
                    "pass_rate": semantic_report.pass_rate,
                    "category_counts": semantic_report.category_counts,
                    "failures": semantic_failures,
                },
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
