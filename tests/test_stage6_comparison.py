"""A service outage cannot be advertised as an algorithm improvement."""

import copy

import pytest

from generaldataagent.evaluation.stage_six import summarize
from generaldataagent.evaluation.stage_six_comparison import compare_runs


def report():
    rows = [
        {
            "case_id": str(i),
            "passed": False,
            "failure": "answer_mismatch",
            "difficulty": "easy",
            "elapsed_seconds": 1.0,
            "tokens": 10,
            "sub_hits": 0,
            "sub_total": 1,
            "cost_usd": 0.001,
        }
        for i in range(10)
    ]
    return dict(
        cohort_hash="c",
        protocol_hash="p",
        source_manifest_hash="s",
        runner_hash="r",
        benchmark="bird",
        model="m",
        results=rows,
        summary=summarize(rows),
    )


def test_complete_pairs_compute_direction_and_exact_p_value():
    a = report()
    b = copy.deepcopy(a)
    for row in b["results"][:6]:
        row.update(passed=True, failure=None, sub_hits=1)
    b["summary"] = summarize(b["results"])
    result = compare_runs(a, b)
    assert result["attribution_valid"]
    assert len(result["improvements"]) == 6
    assert result["mcnemar_exact_two_sided_p"] == pytest.approx(0.03125)


def test_service_confounding_preserves_denominator_and_suppresses_inference():
    a = report()
    b = copy.deepcopy(a)
    a["results"][0]["failure"] = "model_error"
    a["summary"] = summarize(a["results"])
    result = compare_runs(a, b)
    assert result["n"] == 10
    assert not result["attribution_valid"]
    assert result["mcnemar_exact_two_sided_p"] is None
    assert result["service_failures"]["baseline"] == 1


def test_efficiency_and_failure_transitions_preserve_unknown_usage():
    a, b = report(), report()
    b["results"][0].update(failure="token_budget", tokens=20, cost_usd=None)
    b["summary"] = summarize(b["results"])
    result = compare_runs(a, b)
    assert result["efficiency"]["baseline"]["reported_tokens"] == 100
    assert result["efficiency"]["candidate"]["reported_tokens"] == 110
    assert not result["efficiency"]["candidate"]["token_telemetry_complete"]
    assert result["efficiency"]["candidate"]["cost_usd"] is None
    assert result["failure_transitions"]["answer_mismatch->token_budget"] == 1
    assert result["failure_transitions"]["answer_mismatch->answer_mismatch"] == 9
