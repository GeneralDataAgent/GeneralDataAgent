"""Deterministic resource/experience summaries over declared end-to-end samples.

The summary covers queue time, end-to-end latency, model calls, measured and
estimated usage, and cost per successful task. It never treats missing usage as
zero and never turns a research comparison into production acceptance.
"""

from __future__ import annotations

import math
import statistics

from generaldataagent.contracts.resource_experience import ResourceExperienceSample


def _percentile(values: list[int], fraction: float) -> int:
    ordered = sorted(values)
    return ordered[math.ceil(fraction * len(ordered)) - 1]


def _measured(sample: ResourceExperienceSample):
    value = sample.measured
    return value if value is not None and value.complete else None


def _estimated(sample: ResourceExperienceSample):
    value = sample.estimated
    if value is None:
        return None
    values = (
        value.tokens,
        value.tool_calls,
        value.scan_bytes,
        value.cost_microunits,
        value.runtime_milliseconds,
    )
    return value if all(item is not None for item in values) else None


def _total(samples: list[ResourceExperienceSample], field: str, getter) -> int | None:
    values = [getter(sample) for sample in samples]
    if any(value is None for value in values):
        return None
    return sum(getattr(value, field) for value in values)


def _ratio(measured: int | None, estimated: int | None) -> float | None:
    if measured is None or estimated is None or estimated <= 0:
        return None
    return measured / estimated


def _check_samples(samples: list[ResourceExperienceSample]) -> None:
    if not isinstance(samples, list) or not samples:
        raise ValueError("resource_samples_empty")
    if any(not isinstance(sample, ResourceExperienceSample) for sample in samples):
        raise ValueError("resource_samples_invalid")
    task_ids = [sample.task_id for sample in samples]
    if len(set(task_ids)) != len(task_ids):
        raise ValueError("resource_samples_duplicate_task")


def summarize_resource_experience(samples: list[ResourceExperienceSample]) -> dict:
    """Summarize one declared sample; incomplete telemetry yields unknown totals."""
    _check_samples(samples)
    passed = sum(sample.passed for sample in samples)
    latencies = [sample.latency_ms for sample in samples]
    queues = [sample.queue_ms for sample in samples]
    measured = [_measured(sample) for sample in samples]
    estimated = [_estimated(sample) for sample in samples]
    complete_measured = all(value is not None for value in measured)
    complete_estimated = all(value is not None for value in estimated)

    measured_tokens = _total(samples, "tokens", _measured) if complete_measured else None
    measured_cost = _total(samples, "cost_microunits", _measured) if complete_measured else None
    measured_runtime = (
        _total(samples, "runtime_milliseconds", _measured) if complete_measured else None
    )
    estimated_tokens = _total(samples, "tokens", _estimated) if complete_estimated else None
    estimated_cost = _total(samples, "cost_microunits", _estimated) if complete_estimated else None
    estimated_runtime = (
        _total(samples, "runtime_milliseconds", _estimated) if complete_estimated else None
    )
    cost_per_success = (
        measured_cost / passed
        if complete_measured and measured_cost is not None and passed > 0
        else None
    )
    return {
        "n": len(samples),
        "passed": passed,
        "success_rate": passed / len(samples),
        "p50_latency_ms": int(statistics.median(latencies)),
        "p95_latency_ms": _percentile(latencies, 0.95),
        "p50_queue_ms": int(statistics.median(queues)),
        "p95_queue_ms": _percentile(queues, 0.95),
        "queue_share_of_latency": sum(queues) / sum(latencies) if sum(latencies) else None,
        "model_calls_total": sum(sample.model_calls for sample in samples),
        "measured_usage_complete": complete_measured,
        "measured_usage_tasks": sum(value is not None for value in measured),
        "unknown_usage_tasks": sum(value is None for value in measured),
        "measured_tokens_total": measured_tokens,
        "measured_tool_calls_total": (
            _total(samples, "tool_calls", _measured) if complete_measured else None
        ),
        "measured_cost_microunits_total": measured_cost,
        "measured_runtime_milliseconds_total": measured_runtime,
        "estimated_usage_complete": complete_estimated,
        "estimated_tokens_total": estimated_tokens,
        "estimated_cost_microunits_total": estimated_cost,
        "estimated_runtime_milliseconds_total": estimated_runtime,
        "cost_per_success_microunits": cost_per_success,
        "measured_to_estimated": {
            "tokens": _ratio(measured_tokens, estimated_tokens),
            "cost": _ratio(measured_cost, estimated_cost),
            "runtime": _ratio(measured_runtime, estimated_runtime),
        },
        "declared_sample_only": True,
        "production_accepted": False,
    }


def compare_resource_experience(
    baseline: list[ResourceExperienceSample],
    candidate: list[ResourceExperienceSample],
    *,
    min_relative_improvement: float = 0.05,
    max_relative_regression: float = 0.02,
) -> dict:
    """Compare two arms on the same declared tasks; no causal or production claim."""
    if not 0 < min_relative_improvement < 1 or not 0 <= max_relative_regression < 1:
        raise ValueError("resource_comparison_threshold_invalid")
    _check_samples(baseline)
    _check_samples(candidate)
    baseline_ids = {sample.task_id for sample in baseline}
    candidate_ids = {sample.task_id for sample in candidate}
    if baseline_ids != candidate_ids:
        raise ValueError("resource_comparison_task_set_mismatch")

    base = summarize_resource_experience(baseline)
    cand = summarize_resource_experience(candidate)
    latency_ratio = cand["p95_latency_ms"] / base["p95_latency_ms"]
    cost_ratio = None
    if (
        base["cost_per_success_microunits"] is not None
        and cand["cost_per_success_microunits"] is not None
    ):
        cost_ratio = (
            cand["cost_per_success_microunits"] / base["cost_per_success_microunits"]
            if base["cost_per_success_microunits"] > 0
            else None
        )

    improved = []
    if latency_ratio <= 1 - min_relative_improvement:
        improved.append("p95_latency")
    if cost_ratio is not None and cost_ratio <= 1 - min_relative_improvement:
        improved.append("cost_per_success")
    regressed = latency_ratio > 1 + max_relative_regression or (
        cost_ratio is not None and cost_ratio > 1 + max_relative_regression
    )
    candidate_preferred = (
        cand["success_rate"] >= base["success_rate"] and bool(improved) and not regressed
    )
    return {
        "baseline": base,
        "candidate": cand,
        "deltas": {
            "success_rate": cand["success_rate"] - base["success_rate"],
            "p95_latency_ms": cand["p95_latency_ms"] - base["p95_latency_ms"],
            "p95_latency_ratio": latency_ratio,
            "cost_per_success_microunits": (
                cand["cost_per_success_microunits"] - base["cost_per_success_microunits"]
                if cost_ratio is not None
                else None
            ),
            "cost_per_success_ratio": cost_ratio,
        },
        "preferred_arm": "candidate" if candidate_preferred else "baseline",
        "improved_endpoints": improved,
        "regressed": regressed,
        "thresholds": {
            "min_relative_improvement": min_relative_improvement,
            "max_relative_regression": max_relative_regression,
        },
        "causal_attribution": False,
        "declared_sample_only": True,
        "production_accepted": False,
    }
