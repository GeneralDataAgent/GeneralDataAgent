"""Repeated outcomes require independently recorded matching runs and preserve every task."""

import copy

import pytest

from generaldataagent.evaluation.stage_six import summarize
from generaldataagent.evaluation.stage_six_stability import repeated_evidence


def report(run):
    rows = [
        dict(
            case_id=str(i),
            passed=True,
            failure=None,
            difficulty="easy",
            elapsed_seconds=1.0,
            tokens=10,
            sub_hits=1,
            sub_total=1,
            cost_usd=0.001,
            database_hash="d",
        )
        for i in range(10)
    ]
    return dict(
        benchmark="dabstep-dev",
        cohort_hash="c",
        protocol_hash="p",
        source_manifest_hash="s",
        runner_hash="r",
        model="m",
        system_fingerprint={"agent": "a"},
        model_route={"model": "m"},
        completed_at=run,
        results=rows,
        summary=summarize(rows),
    )


def test_three_matching_runs_record_actual_stability():
    result = repeated_evidence([report("a"), report("b"), report("c")])
    assert result["qualified"]
    assert result["all_runs_pass_rate"] == 1
    assert result["flip_rate"] == 0
    assert result["pass_rate_population_variance"] == 0
    assert not result["production_accepted"]


def test_duplicates_and_subject_changes_are_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        repeated_evidence([report("a"), report("a")])
    other = report("b")
    other["system_fingerprint"] = {"agent": "changed"}
    with pytest.raises(ValueError, match="incomparable"):
        repeated_evidence([report("a"), other])


def test_unstable_and_failed_calls_remain_in_denominator():
    a, b, c = report("a"), report("b"), report("c")
    for row in b["results"][:2]:
        row.update(passed=False, failure="model_error", sub_hits=0)
    b["summary"] = summarize(b["results"])
    result = repeated_evidence([a, b, c])
    assert result["n"] == 10
    assert result["flip_rate"] == 0.2
    assert result["all_runs_pass_rate"] == 0.8
    assert not result["qualified"]
    assert "service_unreliable" in result["blocked_by"]


def test_missing_repeat_and_database_change_block_qualification():
    result = repeated_evidence([report("a"), report("b")])
    assert "fewer_than_three_runs" in result["blocked_by"]
    other = copy.deepcopy(report("b"))
    other["results"][0]["database_hash"] = "changed"
    with pytest.raises(ValueError, match="snapshot changed"):
        repeated_evidence([report("a"), other])
