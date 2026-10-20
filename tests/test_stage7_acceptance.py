"""Full pairs and historical correctness cannot be claimed from a small or changed cohort."""

import copy

import pytest

from generaldataagent.evaluation.stage_seven_acceptance import historical_protection, qualify_stage7
from generaldataagent.evaluation.stage_six import summarize


def report():
    rows = [
        {
            "case_id": str(i),
            "passed": True,
            "failure": None,
            "difficulty": "easy",
            "elapsed_seconds": 1.0,
            "tokens": 10,
            "sub_hits": 1,
            "sub_total": 1,
            "cost_usd": None,
            "usage_status": "complete",
            "database_hash": "db",
        }
        for i in range(2)
    ]
    return {
        "benchmark": "bird-mini-dev-sqlite",
        "cohort_hash": "c",
        "protocol_hash": "p",
        "source_manifest_hash": "s",
        "runner_hash": "r",
        "model": "m",
        "results": rows,
        "summary": summarize(rows),
    }


def test_historical_correct_guard_preserves_all_rows_and_does_not_claim_causal_attribution():
    before = report()
    after = copy.deepcopy(before)
    after["protocol_hash"] = "new_protocol"
    after["results"][1].update(passed=False, failure="answer_mismatch", sub_hits=0)
    after["summary"] = summarize(after["results"])
    guard = historical_protection(before, after)
    assert guard["n"] == 2 and guard["regressions"] == ["1"]
    assert not guard["passed"] and not guard["causal_attribution"]
    assert not guard["protocols_equal"]
    after["results"][1]["database_hash"] = "different"
    with pytest.raises(ValueError, match="database"):
        historical_protection(before, after)


def test_small_sample_or_missing_track_cannot_close_full_acceptance():
    with pytest.raises(ValueError, match="all three"):
        qualify_stage7([report()], [report()], [report()])
    with pytest.raises(ValueError, match="all three"):
        qualify_stage7([report()] * 3, [report()] * 3, [report()] * 3)
