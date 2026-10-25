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
        run_status="completed",
        requested_n=10,
        population_n=10,
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


@pytest.mark.parametrize("run_index", [0, 1, 2])
def test_severe_incident_in_any_repeat_cannot_be_offset_by_stability(run_index):
    reports = [report("a"), report("b"), report("c")]
    affected = reports[run_index]
    affected["results"][0].update(
        passed=False, failure="database_mutated", sub_hits=0, severe_incident=True
    )
    affected["summary"] = summarize(affected["results"])
    before = copy.deepcopy(reports)
    result = repeated_evidence(reports)
    assert result["qualified"] is False
    assert result["blocked_by"] == ["severe_incident"]
    assert result["n"] == 10 and result["repeats"] == 3
    assert result["flip_rate"] == 0.1
    assert result["worst_pass_rate"] == 0.9
    assert reports == before


def test_release_diagnostics_keeps_incident_in_an_older_repeat_as_a_blocker():
    from generaldataagent.evaluation.stage_six_gates import release_diagnostics

    current, older, third = report("current"), report("older"), report("third")
    older["results"][0].update(
        passed=False, failure="database_mutated", sub_hits=0, severe_incident=True
    )
    older["summary"] = summarize(older["results"])
    result = release_diagnostics([current], repeated_runs=[older, third])
    stability = result["stability"]["dabstep-dev"]
    assert stability["qualified"] is False
    assert "severe_incident" in stability["blocked_by"]
    assert "repeat_stability_not_qualified" in result["blocked_by"]
    assert result["production_accepted"] is False


@pytest.mark.parametrize("run_index", [0, 1, 2])
def test_repeat_cannot_qualify_after_truncating_the_planned_denominator(run_index):
    reports = [report("a"), report("b"), report("c")]
    reports[run_index].update(requested_n=11, population_n=20)
    before = copy.deepcopy(reports)
    result = repeated_evidence(reports)
    assert result["qualified"] is False
    assert result["blocked_by"] == ["incomplete_planned_cohort"]
    assert result["n"] == 10 and result["mean_pass_rate"] == 1
    assert reports == before


@pytest.mark.parametrize("field", ["requested_n", "population_n"])
@pytest.mark.parametrize("value", [None, 0, -1, True, 10.0, "10"])
def test_unknown_or_invalid_repeat_denominator_cannot_qualify(field, value):
    reports = [report("a"), report("b"), report("c")]
    if value is None:
        reports[1].pop(field)
    else:
        reports[1][field] = value
    result = repeated_evidence(reports)
    assert result["qualified"] is False
    assert result["blocked_by"] == ["incomplete_planned_cohort"]
    assert result["n"] == 10


@pytest.mark.parametrize("requested,population", [(10, 20), (20, 10)])
def test_repeat_planned_denominator_is_capped_by_available_population(requested, population):
    reports = [report("a"), report("b"), report("c")]
    for item in reports:
        item.update(requested_n=requested, population_n=population)
    assert repeated_evidence(reports)["qualified"] is True


def test_release_diagnostics_blocks_an_incomplete_older_repeat():
    from generaldataagent.evaluation.stage_six_gates import release_diagnostics

    current, older, third = report("current"), report("older"), report("third")
    older.update(requested_n=11, population_n=20)
    result = release_diagnostics([current], repeated_runs=[older, third])
    stability = result["stability"]["dabstep-dev"]
    assert stability["qualified"] is False
    assert stability["blocked_by"] == ["incomplete_planned_cohort"]
    assert stability["n"] == 10
    assert "repeat_stability_not_qualified" in result["blocked_by"]
    assert result["production_accepted"] is False


@pytest.mark.parametrize("run_index", [0, 1, 2])
@pytest.mark.parametrize("status", [None, "running", "blocked_by_service", "", True, {}])
def test_unfinished_or_unknown_run_status_cannot_qualify(run_index, status):
    reports = [report("a"), report("b"), report("c")]
    if status is None:
        reports[run_index].pop("run_status")
    else:
        reports[run_index]["run_status"] = status
    before = copy.deepcopy(reports)
    result = repeated_evidence(reports)
    assert result["qualified"] is False
    assert result["blocked_by"] == ["incomplete_run_status"]
    assert result["n"] == 10 and result["repeats"] == 3
    assert result["mean_pass_rate"] == 1 and result["flip_rate"] == 0
    assert reports == before


def test_release_diagnostics_blocks_an_unfinished_older_repeat():
    from generaldataagent.evaluation.stage_six_gates import release_diagnostics

    current, older, third = report("current"), report("older"), report("third")
    older["run_status"] = "blocked_by_service"
    result = release_diagnostics([current], repeated_runs=[older, third])
    stability = result["stability"]["dabstep-dev"]
    assert stability["qualified"] is False
    assert stability["blocked_by"] == ["incomplete_run_status"]
    assert stability["n"] == 10
    assert "repeat_stability_not_qualified" in result["blocked_by"]
    assert result["production_accepted"] is False


@pytest.mark.parametrize("run_index", [0, 1, 2])
@pytest.mark.parametrize("field", ["runner_hash", "model"])
def test_repeat_execution_provenance_must_match_in_every_position(run_index, field):
    reports = [report("a"), report("b"), report("c")]
    reports[run_index][field] = "different"
    before = copy.deepcopy(reports)
    with pytest.raises(ValueError, match="incomparable repeated-run " + field):
        repeated_evidence(reports)
    assert reports == before


@pytest.mark.parametrize("field", ["runner_hash", "model"])
def test_release_diagnostics_refuses_mixed_repeat_execution_provenance(field):
    from generaldataagent.evaluation.stage_six_gates import release_diagnostics

    current, older, third = report("current"), report("older"), report("third")
    older[field] = "different"
    with pytest.raises(ValueError, match="incomparable repeated-run " + field):
        release_diagnostics([current], repeated_runs=[older, third])


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
