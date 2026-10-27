"""Paired repeats are paired by task, and only real repeats count as repeats."""

import copy
import json

import pytest

from generaldataagent.evaluation.repeat_comparison import compare_repeated_runs
from generaldataagent.evaluation.stage_six import summarize

CASES = [str(i) for i in range(1, 11)]


def make_report(
    passed_ids,
    *,
    fingerprint="fp-1",
    protocol="p-1",
    cohort="cohort-1",
    cost=0.0,
    failure="answer_mismatch",
    stamp="t0",
):
    results = []
    for index, case_id in enumerate(CASES):
        passed = case_id in passed_ids
        results.append(
            {
                "case_id": case_id,
                "passed": passed,
                "difficulty": "easy",
                "elapsed_seconds": 1.0 + index,
                "tokens": 100,
                "usage_status": "complete",
                "sub_hits": 1 if passed else 0,
                "sub_total": 1,
                "failure": None if passed else failure,
                "severe_incident": False,
                "cost_usd": cost,
                "database_hash": "db",
                "attempted": True,
            }
        )
    return {
        "benchmark": "dabstep-dev",
        "cohort_hash": cohort,
        "protocol_hash": protocol,
        "source_manifest_hash": "src-1",
        "runner_hash": "runner-1",
        "model": "model-1",
        "model_route": "route-1",
        "system_fingerprint": {"stage9_sources": {"evaluation/stage_nine.py": fingerprint}},
        "run_status": "completed",
        "started_at": "2026-10-28T00:00:00Z" if stamp == "t0" else f"2026-10-28T00:00:0{stamp}Z",
        "requested_n": len(CASES),
        "population_n": len(CASES),
        "results": results,
        "summary": summarize(results),
    }


def arm(passed_ids, *, runs=3, **kwargs):
    # Each recorded run differs in its own timestamp, as real runs do; otherwise
    # the stability check rightly refuses them as one copied report.
    return [make_report(passed_ids, stamp=str(index), **kwargs) for index in range(runs)]


BASE = {"2", "4", "6", "8"}
WIDER = BASE | {"1"}


def compare(baseline, candidate, **kwargs):
    return compare_repeated_runs(baseline, candidate, bootstrap_samples=500, **kwargs)


def test_each_task_is_paired_across_repeats_not_counted_per_run():
    result = compare(arm(BASE), arm(WIDER))
    rows = {row["case_id"]: row for row in result["per_case"]}
    assert rows["1"]["outcome"] == "gain"
    assert rows["1"]["baseline_passes"] == 0 and rows["1"]["candidate_passes"] == 3
    assert rows["2"]["outcome"] == "stable"
    assert result["comparison"]["gains"] == 1
    assert result["comparison"]["regressions"] == 0
    assert result["comparison"]["pass_rate_delta"] == pytest.approx(0.1)
    assert [row["case_id"] for row in result["per_case"]] == CASES


def test_a_single_run_arm_is_reported_as_insufficient_not_silently_accepted():
    result = compare(arm(BASE, runs=1), arm(WIDER, runs=1))
    assert "baseline:single_run_arm" in result["design_blockers"]
    assert "candidate:single_run_arm" in result["design_blockers"]
    assert result["criteria_met"] is False
    # The per-task evidence is still reported; only the claim is withheld.
    assert result["comparison"]["gains"] == 1


def test_two_repeats_are_still_below_the_exit_criterion():
    result = compare(arm(BASE, runs=2), arm(WIDER, runs=2))
    assert "baseline:fewer_than_three_runs" in result["design_blockers"]
    assert "candidate:fewer_than_three_runs" in result["design_blockers"]


def test_a_changed_cohort_is_refused_rather_than_averaged():
    with pytest.raises(ValueError, match="cohort_hash"):
        compare(arm(BASE), arm(WIDER, cohort="cohort-2"))


def test_a_system_change_within_an_arm_is_refused():
    baseline = arm(BASE)
    baseline[1] = make_report(BASE, fingerprint="fp-2", stamp="1")
    with pytest.raises(ValueError, match="system_fingerprint"):
        compare(baseline, arm(WIDER))


def test_a_copied_report_is_not_an_independent_repeat():
    baseline = arm(BASE, runs=2)
    baseline[1] = copy.deepcopy(baseline[0])
    with pytest.raises(ValueError, match="duplicate"):
        compare(baseline, arm(WIDER))


def test_unmeasured_cost_is_reported_as_unknown_never_as_zero():
    result = compare(arm(BASE), arm(WIDER, cost=None))
    assert result["arms"]["candidate"]["cost_verified"] is False
    assert result["arms"]["candidate"]["cost_usd"] is None
    assert "cost_not_verified" in result["design_blockers"]


def test_provider_failures_block_attribution():
    failure = make_report(WIDER, failure="model_error", stamp="9")
    candidate = [failure] + arm(WIDER, runs=2)
    result = compare(arm(BASE), candidate)
    # Five failed task-runs out of thirty: the provider, not the treatment,
    # explains part of the candidate's outcome.
    assert result["service_error_rates"]["candidate"] == pytest.approx(5 / 30)
    assert "service_unreliable" in result["design_blockers"]


def test_the_interval_is_deterministic_and_orders_the_delta():
    first = compare(arm(BASE), arm(WIDER))
    second = compare(arm(BASE), arm(WIDER))
    interval = first["comparison"]["bootstrap_95"]
    assert interval == second["comparison"]["bootstrap_95"]
    assert interval[0] <= first["comparison"]["pass_rate_delta"] <= interval[1]


def test_a_development_cohort_never_meets_the_exit_criterion():
    result = compare(arm(set()), arm(set(CASES)))
    assert result["design_blockers"] == ["cohort_not_held_out"]
    assert result["criteria_met"] is False


def test_a_held_out_cohort_with_a_clear_gain_meets_only_the_internal_criteria():
    result = compare(arm(set()), arm(set(CASES)), cohort_status="held_out")
    assert result["design_blockers"] == []
    assert result["criteria_met"] is True
    assert result["independent_review"] == "not_supplied"
    assert result["production_accepted"] is False
    assert json.dumps(result)  # the result stays serialisable evidence
