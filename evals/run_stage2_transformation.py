"""Run the frozen Stage 2 transformation-safety matrix."""

import json
from pathlib import Path
from typing import Any

from generaldataagent.contracts import TransformationTest
from generaldataagent.data_engineering import build_transformation_proposal

DATASET = Path(__file__).parent / "datasets" / "stage2-transformation.jsonl"


def run_transformation_evaluation(dataset_path: Path = DATASET) -> dict[str, Any]:
    cases = [json.loads(line) for line in dataset_path.read_text().splitlines() if line.strip()]
    failures: list[dict[str, str]] = []
    accepted = 0
    rejected = 0
    for case in cases:
        expected_status = case["expected"]["status"]
        try:
            result = build_transformation_proposal(
                connector_id="evaluation",
                dialect="sqlite",
                target_relation="proposed_orders",
                select_sql=case["sql"],
                allowed_relations=set(case["allowed"]),
                tests=(
                    (TransformationTest(name="has-rows", assertion="row_count_nonzero"),)
                    if case.get("tests", True)
                    else ()
                ),
                impact_summary=case.get("impact_summary", "Frozen safety evaluation."),
                rollback_plan=case.get("rollback_plan", "Discard the dry-run proposal."),
            )
        except ValueError as exc:
            if expected_status == "rejected":
                rejected += 1
                continue
            failures.append(
                {
                    "case_id": case["case_id"],
                    "error": f"unexpected rejection: {exc}",
                }
            )
            continue
        if expected_status != "accepted":
            failures.append(
                {
                    "case_id": case["case_id"],
                    "error": "unsafe proposal was accepted",
                }
            )
            continue
        accepted += 1
        expected_dependencies = case["expected"]["dependencies"]
        if list(result.dependencies) != expected_dependencies or not result.dry_run_only:
            failures.append(
                {
                    "case_id": case["case_id"],
                    "error": "accepted proposal did not match dependencies or dry-run contract",
                }
            )
    passed = len(cases) - len(failures)
    return {
        "dataset_size": len(cases),
        "accepted_cases": accepted,
        "rejected_cases": rejected,
        "passed": passed,
        "failed": len(failures),
        "pass_rate": passed / len(cases) if cases else 0.0,
        "failures": failures,
    }


if __name__ == "__main__":
    result = run_transformation_evaluation()
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0 if result["failed"] == 0 else 1)
