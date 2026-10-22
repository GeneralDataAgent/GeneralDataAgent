import copy

import pytest

from generaldataagent.evaluation.stage_eight_review import review_stage8


def test_stage8_rejects_old_incomplete_and_different_routes():
    report = {"protocol": {"version": "stage7-v1"}, "run_status": "completed"}
    with pytest.raises(ValueError, match="Stage 8"):
        review_stage8(report, report)
    report["protocol"]["version"] = "stage8-v1"
    report["run_status"] = "interrupted"
    with pytest.raises(ValueError, match="completed"):
        review_stage8(report, report)
    report["run_status"] = "completed"
    with pytest.raises(ValueError, match="route"):
        review_stage8(report, report | {"model_route": {"model": "other"}})


def test_stage8_protects_history_without_awarding_scores(monkeypatch):
    monkeypatch.setattr(
        "generaldataagent.evaluation.stage_eight_review.compare_runs",
        lambda a, b: {"baseline_passed": 1, "candidate_passed": 0},
    )
    before = {
        "protocol": {"version": "stage8-v1"},
        "run_status": "completed",
        "benchmark": "test",
        "results": [{"case_id": "a", "passed": True, "database_hash": "fixed"}],
    }
    after = copy.deepcopy(before)
    after["results"][0]["passed"] = False
    historic = {
        "benchmark": "test",
        "results": [{"case_id": "a", "passed": True}, {"case_id": "missing", "passed": True}],
    }
    snapshot = copy.deepcopy((before, after, historic))
    result = review_stage8(before, after, historic)
    assert result["fresh_correct_protection"]["regressed"] == ["a"]
    assert result["historical_correct_protection"] == {
        "n": 2,
        "missing": ["missing"],
        "regressed": ["a"],
    }
    assert result["strict_comparison"]["candidate_passed"] == 0
    assert not result["strict_score_changed"]
    assert (before, after, historic) == snapshot
    after["results"][0]["database_hash"] = "different"
    with pytest.raises(ValueError, match="database"):
        review_stage8(before, after)
