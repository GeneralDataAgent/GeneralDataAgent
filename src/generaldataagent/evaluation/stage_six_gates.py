"""Fail-closed release diagnostics for public development evaluation evidence."""

import math
from collections import Counter

from generaldataagent.evaluation.stage_six import paired_gate, summarize

REQUIRED_TRACKS = {
    "bird-mini-dev-sqlite": 60,
    "infiagent-dabench": 60,
    "dabstep-dev": 10,
}


def validate_report(report: dict) -> dict:
    """Recompute statistics; reject duplicates, nonfinite telemetry and missing provenance."""
    for key in ("cohort_hash", "protocol_hash", "source_manifest_hash", "runner_hash", "model"):
        if not report.get(key):
            raise ValueError(f"missing provenance: {key}")
    results = report["results"]
    for row in results:
        if not isinstance(row["passed"], bool):
            raise ValueError("passed must be a boolean")
        if not math.isfinite(row["elapsed_seconds"]) or row["elapsed_seconds"] < 0:
            raise ValueError("invalid latency")
        if type(row["tokens"]) is not int or row["tokens"] < 0:
            raise ValueError("invalid token telemetry")
        cost = row.get("cost_usd")
        if cost is not None and (
            type(cost) not in {int, float} or not math.isfinite(cost) or cost < 0
        ):
            raise ValueError("invalid cost telemetry")
        if row["passed"] and (row["failure"] or row.get("severe_incident")):
            raise ValueError("contradictory success")
    expected = summarize(results)
    if expected != report["summary"]:
        raise ValueError("summary does not match case-level evidence")
    return expected


def release_diagnostics(
    reports: list[dict],
    *,
    baselines: list[dict] | None = None,
    min_success_rate: float = 0.8,
    max_p95_seconds: float = 120,
    max_model_error_rate: float = 0.05,
) -> dict:
    """Provisional internal gates, not benchmark-author thresholds or production approval."""
    if not 0 <= min_success_rate <= 1 or not 0 <= max_model_error_rate <= 1 or max_p95_seconds <= 0:
        raise ValueError("invalid release policy")
    tracks = {}
    blocks = []
    baseline_map = {r["benchmark"]: r for r in baselines or []}
    if len(baseline_map) != len(baselines or []):
        raise ValueError("duplicate baseline track")
    for report in reports:
        name = report["benchmark"]
        if name not in REQUIRED_TRACKS or name in tracks:
            raise ValueError("unknown or duplicate track")
        summary = validate_report(report)
        failures = Counter(x["failure"] for x in report["results"])
        model_failures = sum(
            failures[k] for k in ["model_error", "worker_error", "worker_deadline"]
        )
        reasons = []
        if summary["n"] < REQUIRED_TRACKS[name]:
            reasons.append("insufficient_coverage")
        if summary["pass_at_1"] < min_success_rate:
            reasons.append("quality_below_provisional_target")
        if summary["p95_seconds"] > max_p95_seconds:
            reasons.append("latency_budget_exceeded")
        if model_failures / summary["n"] > max_model_error_rate:
            reasons.append("model_service_unreliable")
        if any(x.get("severe_incident") for x in report["results"]):
            reasons.append("severe_incident")
        if any(x.get("cost_usd") is None for x in report["results"]):
            reasons.append("cost_not_verified")
        paired = None
        if name in baseline_map:
            validate_report(baseline_map[name])
            if baseline_map[name]["source_manifest_hash"] != report["source_manifest_hash"]:
                raise ValueError("incomparable source snapshot")
            paired = paired_gate(baseline_map[name], report)
            if not paired["passed"]:
                reasons.append("paired_regression")
        else:
            reasons.append("paired_baseline_not_supplied")
        tracks[name] = {"summary": summary, "blocked_by": reasons, "paired": paired}
        blocks.extend(f"{name}:{reason}" for reason in reasons)
    for name in REQUIRED_TRACKS.keys() - tracks.keys():
        blocks.append(name + ":missing_track")
    blocks.extend(
        [
            "repeat_stability_not_qualified",
            "independent_human_review_not_supplied",
            "production_supervisor_not_evaluated",
            "shadow_and_canary_not_qualified",
        ]
    )
    return {
        "passed": not blocks,
        "production_accepted": False,
        "tracks": tracks,
        "blocked_by": blocks,
        "policy": {
            "min_success_rate": min_success_rate,
            "max_p95_seconds": max_p95_seconds,
            "max_model_error_rate": max_model_error_rate,
            "status": "provisional internal thresholds; no official ranking claim",
        },
    }
