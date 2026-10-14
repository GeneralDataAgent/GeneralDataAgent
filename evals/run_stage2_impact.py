"""Run the frozen Stage 2 impact-analysis matrix."""

import json
from pathlib import Path
from typing import Any

from generaldataagent.contracts import LineageAsset
from generaldataagent.data_engineering import analyze_impact

DATASET = Path(__file__).parent / "datasets" / "stage2-impact.jsonl"


def run_impact_evaluation(dataset_path: Path = DATASET) -> dict[str, Any]:
    cases = [json.loads(line) for line in dataset_path.read_text().splitlines() if line.strip()]
    failures: list[dict[str, str]] = []
    for case in cases:
        try:
            result = analyze_impact(
                tuple(
                    LineageAsset(
                        asset_id=asset_id,
                        asset_type=asset_type,
                        depends_on=tuple(depends_on),
                    )
                    for asset_id, asset_type, depends_on in case["assets"]
                ),
                tuple(case["changed"]),
            )
            actual = [
                [item.asset_id, item.asset_type, item.distance, list(item.path)]
                for item in result.impacted_assets
            ]
            assert actual == case["expected"]["assets"]
            assert result.has_cycles == case["expected"]["cycles"]
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


if __name__ == "__main__":
    result = run_impact_evaluation()
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0 if result["failed"] == 0 else 1)
