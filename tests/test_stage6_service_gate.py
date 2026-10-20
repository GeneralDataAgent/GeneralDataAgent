"""Service calibration must reject failing batches without filtering on answer quality."""

import pytest
from test_stage6_release_gates import report

from generaldataagent.evaluation.stage_six import summarize
from generaldataagent.evaluation.stage_six_control import service_readiness


def test_healthy_responses_are_ready_even_when_answers_are_wrong():
    r = report(6)
    for row in r["results"]:
        row.update(passed=False, failure="answer_mismatch", sub_hits=0)
    r["summary"] = summarize(r["results"])
    assert service_readiness(r)["ready"]
    assert not service_readiness(r)["production_accepted"]


def test_two_provider_errors_in_six_tasks_block_expansion():
    r = report(6)
    for row in r["results"][:2]:
        row.update(
            passed=False,
            failure="model_error",
            sub_hits=0,
            tokens=0,
            usage_status="partial_or_unknown",
        )
    r["summary"] = summarize(r["results"])
    gate = service_readiness(r)
    assert not gate["ready"] and gate["service_errors"] == 2
    assert gate["observed_model_replies"] == 4
    assert "provider_error_rate_exceeded" in gate["blocked_by"]


def test_small_or_incomplete_calibration_cannot_qualify_a_batch():
    assert not service_readiness(report(3))["ready"]
    r = report(6)
    r["requested_n"] = 60
    assert "incomplete_calibration_cohort" in service_readiness(r)["blocked_by"]
    r["requested_n"] = 6
    r["results"][0].update(attempted=False, tokens=0)
    r["summary"] = summarize(r["results"])
    assert "insufficient_observed_model_replies" in service_readiness(r)["blocked_by"]


def test_tampered_statistics_and_invalid_policy_are_rejected():
    r = report(6)
    r["summary"]["n"] = 99
    with pytest.raises(ValueError):
        service_readiness(r)
    with pytest.raises(ValueError):
        service_readiness(report(6), min_tasks=0)
