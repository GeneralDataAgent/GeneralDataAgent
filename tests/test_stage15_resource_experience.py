"""Stage 15.1D checks declared resource/experience summaries and comparisons."""

import pytest

from generaldataagent.contracts.child_budget import BudgetUsageAmount
from generaldataagent.contracts.resource_experience import (
    EstimatedResource,
    ResourceExperienceSample,
)
from generaldataagent.evaluation.resource_experience import (
    compare_resource_experience,
    summarize_resource_experience,
)


def estimated(tokens=100, cost=100, runtime=2000) -> EstimatedResource:
    return EstimatedResource(
        tokens=tokens,
        tool_calls=2,
        scan_bytes=1000,
        cost_microunits=cost,
        runtime_milliseconds=runtime,
    )


def measured(tokens=40, cost=40, runtime=1000) -> BudgetUsageAmount:
    return BudgetUsageAmount(
        source="provider_reported",
        tokens=tokens,
        tool_calls=1,
        scan_bytes=400,
        cost_microunits=cost,
        runtime_milliseconds=runtime,
        complete=True,
    )


def sample(
    task_id,
    *,
    passed=True,
    queue=10,
    latency=110,
    model_calls=1,
    estimated_value=None,
    measured_value=None,
) -> ResourceExperienceSample:
    return ResourceExperienceSample(
        task_id=task_id,
        passed=passed,
        queue_ms=queue,
        latency_ms=latency,
        model_calls=model_calls,
        estimated=estimated() if estimated_value is None else estimated_value,
        measured=measured() if measured_value is None else measured_value,
    )


def samples(latencies, *, cost=40, passed=True):
    return [
        sample(
            f"task-{index}",
            latency=latency,
            queue=10,
            passed=passed,
            measured_value=measured(cost=cost),
        )
        for index, latency in enumerate(latencies)
    ]


def test_summary_reports_latency_queue_and_measured_cost_per_success():
    report = summarize_resource_experience(samples([100, 110, 120, 130]))

    assert report["n"] == 4
    assert report["passed"] == 4
    assert report["p50_latency_ms"] == 115
    assert report["p95_latency_ms"] == 130
    assert report["p50_queue_ms"] == 10
    assert report["p95_queue_ms"] == 10
    assert report["model_calls_total"] == 4
    assert report["measured_tokens_total"] == 160
    assert report["measured_tool_calls_total"] == 4
    assert report["measured_cost_microunits_total"] == 160
    assert report["cost_per_success_microunits"] == 40
    assert report["measured_usage_complete"] is True
    assert report["production_accepted"] is False


def test_missing_measured_usage_stays_unknown_not_zero():
    rows = samples([100, 110])
    rows[1] = sample(
        "task-1",
        latency=110,
        measured_value=BudgetUsageAmount(source="unknown"),
    )

    report = summarize_resource_experience(rows)

    assert report["measured_usage_complete"] is False
    assert report["unknown_usage_tasks"] == 1
    assert report["measured_tokens_total"] is None
    assert report["measured_cost_microunits_total"] is None
    assert report["cost_per_success_microunits"] is None
    assert report["measured_to_estimated"]["cost"] is None


def test_incomplete_estimate_keeps_estimated_totals_unknown():
    rows = samples([100])
    rows[0] = sample(
        "task-0",
        measured_value=measured(),
        estimated_value=EstimatedResource(tokens=100),
    )

    report = summarize_resource_experience(rows)

    assert report["estimated_usage_complete"] is False
    assert report["estimated_cost_microunits_total"] is None
    assert report["estimated_tokens_total"] is None
    assert report["measured_to_estimated"]["tokens"] is None
    assert report["measured_to_estimated"]["cost"] is None


def test_latency_before_queue_is_refused():
    with pytest.raises(ValueError, match="resource_sample_latency_before_queue"):
        sample("task-a", queue=100, latency=99)


def test_duplicate_and_empty_samples_are_refused():
    with pytest.raises(ValueError, match="resource_samples_empty"):
        summarize_resource_experience([])
    with pytest.raises(ValueError, match="resource_samples_duplicate_task"):
        summarize_resource_experience([sample("task-a"), sample("task-a")])


def test_candidate_with_lower_p95_and_no_regression_is_preferred():
    baseline = samples([100, 110, 120, 130])
    candidate = samples([90, 99, 108, 117])

    comparison = compare_resource_experience(baseline, candidate)

    assert comparison["preferred_arm"] == "candidate"
    assert comparison["improved_endpoints"] == ["p95_latency"]
    assert comparison["regressed"] is False
    assert comparison["causal_attribution"] is False
    assert comparison["production_accepted"] is False


def test_candidate_with_only_a_marginal_latency_gain_keeps_baseline():
    comparison = compare_resource_experience(samples([100, 100]), samples([99, 99]))
    assert comparison["preferred_arm"] == "baseline"
    assert comparison["improved_endpoints"] == []


def test_candidate_that_loses_accuracy_keeps_baseline_even_if_faster():
    baseline = samples([100, 110])
    candidate = samples([80, 88], passed=False)
    comparison = compare_resource_experience(baseline, candidate)
    assert comparison["preferred_arm"] == "baseline"


def test_candidate_cost_improvement_alone_can_prefer_candidate():
    comparison = compare_resource_experience(
        samples([100, 110], cost=40),
        samples([100, 110], cost=30),
    )
    assert comparison["preferred_arm"] == "candidate"
    assert comparison["improved_endpoints"] == ["cost_per_success"]


def test_cost_regression_blocks_a_latency_win():
    comparison = compare_resource_experience(
        samples([100, 110], cost=40),
        samples([80, 88], cost=60),
    )
    assert comparison["preferred_arm"] == "baseline"
    assert comparison["regressed"] is True


def test_comparison_requires_the_same_task_set():
    with pytest.raises(ValueError, match="resource_comparison_task_set_mismatch"):
        compare_resource_experience(samples([100]), [sample("other-task", latency=100)])


def test_invalid_comparison_thresholds_are_refused():
    with pytest.raises(ValueError, match="resource_comparison_threshold_invalid"):
        compare_resource_experience(samples([100]), samples([100]), min_relative_improvement=0)


def test_zero_latency_does_not_divide_by_zero():
    rows = [sample("task-a", queue=0, latency=0)]
    comparison = compare_resource_experience(rows, rows)
    assert comparison["deltas"]["p95_latency_ratio"] is None
    assert comparison["preferred_arm"] == "baseline"
