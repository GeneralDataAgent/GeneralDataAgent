"""Release evidence must fail closed on absent, corrupt and contradictory observations."""

import copy

import pytest

from generaldataagent.evaluation.stage_six import summarize
from generaldataagent.evaluation.stage_six_gates import release_diagnostics, validate_report


def report(n=10, benchmark="dabstep-dev"):
    results = [
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
        }
        for i in range(n)
    ]
    return dict(
        benchmark=benchmark,
        cohort_hash="c",
        protocol_hash="p",
        source_manifest_hash="s",
        runner_hash="r",
        model="m",
        results=results,
        summary=summarize(results),
    )


def test_local_high_scores_never_close_unobserved_production_gates():
    r = report()
    result = release_diagnostics([r])
    assert not result["passed"]
    assert not result["production_accepted"]
    assert "cost_not_verified" in result["tracks"]["dabstep-dev"]["blocked_by"]
    assert any("missing_track" in x for x in result["blocked_by"])
    assert "production_supervisor_not_evaluated" in result["blocked_by"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r["summary"].update(pass_at_1=0.99),
        lambda r: r.update(model=""),
        lambda r: r["results"][0].update(passed="true"),
        lambda r: r["results"][0].update(elapsed_seconds=float("nan")),
        lambda r: r["results"][0].update(tokens=-1),
        lambda r: r["results"][0].update(failure="hidden_error"),
        lambda r: r["results"][0].update(severe_incident=True),
        lambda r: r["results"].append(copy.deepcopy(r["results"][0])),
    ],
)
def test_invalid_evidence_rejected(mutation):
    r = report()
    mutation(r)
    with pytest.raises(ValueError):
        validate_report(r)


def test_complete_tracks_still_require_reviews_repeat_runs_and_live_environment():
    reports = [report(60, "bird-mini-dev-sqlite"), report(60, "infiagent-dabench"), report()]
    result = release_diagnostics(reports, baselines=copy.deepcopy(reports))
    assert len(result["tracks"]) == 3
    assert all(x["paired"]["passed"] for x in result["tracks"].values())
    assert "repeat_stability_not_qualified" in result["blocked_by"]
    assert not result["passed"]


def test_failed_model_calls_stay_in_task_denominator_and_fail_quality():
    r = report()
    for row in r["results"]:
        row.update(passed=False, failure="model_error", sub_hits=0)
    r["summary"] = summarize(r["results"])
    result = release_diagnostics([r])
    assert result["tracks"]["dabstep-dev"]["summary"]["n"] == 10
    assert "model_service_unreliable" in result["tracks"]["dabstep-dev"]["blocked_by"]
    assert "quality_below_provisional_target" in result["tracks"]["dabstep-dev"]["blocked_by"]


def test_duplicate_track_and_incomparable_baseline_rejected():
    r = report()
    with pytest.raises(ValueError):
        release_diagnostics([r, r])
    other = copy.deepcopy(r)
    other["protocol_hash"] = "changed"
    with pytest.raises(ValueError):
        release_diagnostics([r], baselines=[other])


def test_source_changes_and_duplicate_baselines_cannot_be_paired():
    r = report()
    other = copy.deepcopy(r)
    other["source_manifest_hash"] = "different_data"
    with pytest.raises(ValueError, match="source snapshot"):
        release_diagnostics([r], baselines=[other])
    with pytest.raises(ValueError, match="duplicate baseline"):
        release_diagnostics([r], baselines=[r, r])


def test_actual_repeated_runs_are_recomputed_and_bound_to_candidate():
    r = report()
    r.update(
        system_fingerprint={"agent": "a"},
        model_route={"model": "m"},
        completed_at="one",
        run_status="completed",
        requested_n=10,
        population_n=10,
    )
    for row in r["results"]:
        row["database_hash"] = "d"
    second, third = copy.deepcopy(r), copy.deepcopy(r)
    second["completed_at"] = "two"
    third["completed_at"] = "three"
    result = release_diagnostics([r], repeated_runs=[second, third])
    assert result["stability"]["dabstep-dev"]["qualified"]
    assert result["stability"]["dabstep-dev"]["repeats"] == 3
    assert not result["production_accepted"]
    with pytest.raises(ValueError):
        release_diagnostics([r], repeated_runs=[second, second])


@pytest.mark.parametrize(
    "key,value",
    [
        ("elapsed_seconds", True),
        ("elapsed_seconds", "fast"),
        ("tokens", True),
        ("cost_usd", -1),
        ("cost_usd", float("inf")),
        ("sub_hits", -1),
        ("sub_total", 0),
    ],
)
def test_invalid_evidence_cannot_be_summarized_into_acceptance(key, value):
    r = report()
    r["results"][0][key] = value
    with pytest.raises(ValueError):
        validate_report(r)
