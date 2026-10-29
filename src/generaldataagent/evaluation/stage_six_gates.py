"""Fail-closed release diagnostics for public development evaluation evidence."""

from collections import Counter

from generaldataagent.evaluation.run_report import RunReportInput, ValidatedRunReport
from generaldataagent.evaluation.stage_six import paired_gate

REQUIRED_TRACKS = {
    "bird-mini-dev-sqlite": 60,
    "infiagent-dabench": 60,
    "dabstep-dev": 10,
}


def validate_report(report: RunReportInput) -> dict:
    """Compatibility entry point returning a detached, recomputed summary."""
    return ValidatedRunReport.from_report(report).summary


def release_diagnostics(
    reports: list[dict],
    *,
    baselines: list[dict] | None = None,
    min_success_rate: float = 0.8,
    max_p95_seconds: float = 120,
    max_model_error_rate: float = 0.05,
    repeated_runs: list[dict] | None = None,
) -> dict:
    """Provisional internal gates, not benchmark-author thresholds or production approval."""
    if not 0 <= min_success_rate <= 1 or not 0 <= max_model_error_rate <= 1 or max_p95_seconds <= 0:
        raise ValueError("invalid release policy")
    from generaldataagent.evaluation.stage_six_stability import (
        canonical_report_hash,
        repeated_evidence,
    )

    repeat_by_track = {}
    for repeated in repeated_runs or []:
        if repeated["benchmark"] not in REQUIRED_TRACKS:
            raise ValueError("unknown repeated-run track")
        repeat_by_track.setdefault(repeated["benchmark"], []).append(repeated)
    stability = {}
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
            failures[k]
            for k in ["model_error", "worker_error", "worker_deadline", "service_circuit_open"]
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
        if name in repeat_by_track:
            supplied = repeat_by_track[name]
            current_hash = canonical_report_hash(report)
            if sum(canonical_report_hash(r) == current_hash for r in supplied) > 1:
                raise ValueError("duplicated candidate in repeated evidence")
            inputs = [report] + [r for r in supplied if canonical_report_hash(r) != current_hash]
            stability[name] = repeated_evidence(inputs)
        tracks[name] = {"summary": summary, "blocked_by": reasons, "paired": paired}
        blocks.extend(f"{name}:{reason}" for reason in reasons)
    for name in REQUIRED_TRACKS.keys() - tracks.keys():
        blocks.append(name + ":missing_track")
    if not all(stability.get(name, {}).get("qualified", False) for name in REQUIRED_TRACKS):
        blocks.append("repeat_stability_not_qualified")
    blocks.extend(
        [
            "independent_human_review_not_supplied",
            "production_supervisor_not_evaluated",
            "shadow_and_canary_not_qualified",
        ]
    )
    return {
        "passed": not blocks,
        "production_accepted": False,
        "tracks": tracks,
        "stability": stability,
        "blocked_by": blocks,
        "policy": {
            "min_success_rate": min_success_rate,
            "max_p95_seconds": max_p95_seconds,
            "max_model_error_rate": max_model_error_rate,
            "status": "provisional internal thresholds; no official ranking claim",
        },
    }
