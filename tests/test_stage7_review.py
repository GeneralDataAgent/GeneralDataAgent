"""Disputed labels never mutate strict scores or remove denominator cases."""

import copy

import pytest

from generaldataagent.evaluation.stage_seven_review import review_comparison


def test_old_protocol_and_changed_route_are_rejected_before_comparison():
    report = {"protocol": {"version": "stage6-v7"}}
    with pytest.raises(ValueError, match="baseline"):
        review_comparison(report, report, [])
    report["protocol"]["version"] = "stage7-v1"
    other = copy.deepcopy(report) | {"model_route": {"model": "other"}}
    with pytest.raises(ValueError, match="model route"):
        review_comparison(report, other, [])


def test_side_ledger_does_not_award_disputed_credit(monkeypatch):
    monkeypatch.setattr(
        "generaldataagent.evaluation.stage_seven_review.compare_runs",
        lambda before, after: {"baseline_passed": 1, "candidate_passed": 0},
    )
    before = {
        "protocol": {"version": "stage7-v1"},
        "benchmark": "test",
        "results": [{"case_id": "a", "passed": True, "database_hash": "fixed"}],
    }
    after = copy.deepcopy(before)
    after["results"][0]["passed"] = False
    snapshot = copy.deepcopy((before, after))
    review = {
        "benchmark": "test",
        "case_id": "a",
        "issue": "ambiguous entity grain",
        "evidence_refs": ["independent-review.json"],
    }
    result = review_comparison(before, after, [review])
    assert result["strict_comparison"]["candidate_passed"] == 0
    assert result["baseline_correct_protection"]["regressed"] == ["a"]
    assert not result["strict_score_changed"] and not result["denominator_changed"]
    assert (before, after) == snapshot
    with pytest.raises(ValueError, match="duplicate"):
        review_comparison(before, after, [review, review])
