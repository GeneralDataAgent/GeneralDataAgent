"""Paired system comparisons with explicit model-service confounding diagnostics."""

import math
from collections import Counter

from generaldataagent.evaluation.run_report import RunReportInput, ValidatedRunReport
from generaldataagent.evaluation.stage_six import paired_gate

SERVICE_FAILURES = {"model_error", "worker_error", "worker_deadline", "service_circuit_open"}


def compare_runs(
    baseline: RunReportInput, candidate: RunReportInput, *, max_service_error_rate: float = 0.05
) -> dict:
    baseline_run = ValidatedRunReport.from_report(baseline)
    candidate_run = ValidatedRunReport.from_report(candidate)
    before, after = baseline_run.summary, candidate_run.summary
    baseline, candidate = baseline_run.to_dict(), candidate_run.to_dict()
    paired = paired_gate(baseline, candidate)
    old = {r["case_id"]: r for r in baseline["results"]}
    new = {r["case_id"]: r for r in candidate["results"]}
    improvements = [key for key in old if not old[key]["passed"] and new[key]["passed"]]
    regressions = paired["regressions"]
    failures = [
        sum(r["failure"] in SERVICE_FAILURES for r in report["results"])
        for report in [baseline, candidate]
    ]
    blockers = []
    if any(value / before["n"] > max_service_error_rate for value in failures):
        blockers.append("provider_unavailability_confounds_attribution")
    discordant = len(improvements) + len(regressions)
    exact_p = None
    if not blockers:
        tail = sum(
            math.comb(discordant, i) for i in range(min(len(improvements), len(regressions)) + 1)
        )
        exact_p = min(1.0, 2 * tail / 2**discordant) if discordant else 1.0
    observed_cost = [
        sum(row.get("observed_cost_usd", row.get("cost_usd") or 0) for row in report["results"])
        for report in [baseline, candidate]
    ]
    return {
        "benchmark": baseline["benchmark"],
        "n": before["n"],
        "baseline_passed": before["passed"],
        "candidate_passed": after["passed"],
        "improvements": improvements,
        "regressions": regressions,
        "pass_rate_delta": after["pass_at_1"] - before["pass_at_1"],
        "service_failures": {"baseline": failures[0], "candidate": failures[1]},
        "observed_cost_usd": {"baseline": observed_cost[0], "candidate": observed_cost[1]},
        "efficiency": {
            name: {
                "reported_tokens": summary["tokens"],
                "token_telemetry_complete": summary["token_telemetry_complete"],
                "p50_seconds": summary["p50_seconds"],
                "p95_seconds": summary["p95_seconds"],
                "cost_usd": summary["cost_usd"],
            }
            for name, summary in [("baseline", before), ("candidate", after)]
        },
        "failure_transitions": dict(
            Counter(
                (old[key]["failure"] or "passed") + "->" + (new[key]["failure"] or "passed")
                for key in old
            )
        ),
        "attribution_valid": not blockers,
        "blocked_by": blockers,
        "mcnemar_exact_two_sided_p": exact_p,
        "paired_release_gate": paired,
        "production_accepted": False,
    }
