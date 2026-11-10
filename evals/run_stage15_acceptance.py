"""Deterministic Stage 15.1D resource/experience smoke run.

The samples are synthetic and only exercise the measurement contract. This is
not production load, not a live-model comparison, and not acceptance evidence.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from generaldataagent.contracts.child_budget import BudgetUsageAmount
from generaldataagent.contracts.resource_experience import (
    EstimatedResource,
    ResourceExperienceSample,
)
from generaldataagent.evaluation.resource_experience import (
    compare_resource_experience,
    summarize_resource_experience,
)


def _samples(latencies: tuple[int, ...], *, tokens: int) -> list[ResourceExperienceSample]:
    return [
        ResourceExperienceSample(
            task_id=f"sample-{index}",
            passed=True,
            queue_ms=150,
            latency_ms=latency,
            model_calls=2,
            estimated=EstimatedResource(
                tokens=500,
                tool_calls=4,
                scan_bytes=10_000,
                cost_microunits=500,
                runtime_milliseconds=2_000,
            ),
            measured=BudgetUsageAmount(
                source="provider_reported",
                tokens=tokens,
                tool_calls=3,
                scan_bytes=8_000,
                cost_microunits=400,
                runtime_milliseconds=latency - 150,
                complete=True,
            ),
        )
        for index, latency in enumerate(latencies)
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path(".artifacts/stage15-acceptance"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    baseline = _samples((1_000, 1_100, 1_200, 1_300, 1_400), tokens=400)
    candidate = _samples((900, 990, 1_080, 1_170, 1_260), tokens=360)
    baseline_summary = summarize_resource_experience(baseline)
    comparison = compare_resource_experience(baseline, candidate)
    passed = (
        baseline_summary["measured_usage_complete"]
        and baseline_summary["cost_per_success_microunits"] is not None
        and comparison["preferred_arm"] == "candidate"
        and comparison["causal_attribution"] is False
        and comparison["production_accepted"] is False
    )
    result = {
        "passed": passed,
        "sample_kind": "synthetic_contract_smoke",
        "baseline": baseline_summary,
        "comparison": comparison,
        "independent_review": "not_run",
        "production_accepted": False,
    }
    (args.output / "stage15-resource-experience.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps({"passed": passed, "output": str(args.output)}))
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
