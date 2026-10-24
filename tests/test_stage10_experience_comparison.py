"""Treatment comparisons retain complete cohorts without rewriting either report."""

import copy
import hashlib
import json

import pytest

from generaldataagent.evaluation.experience_comparison import compare_experience_runs
from generaldataagent.evaluation.stage_six import paired_gate, summarize


def protocol_hash(report):
    report["protocol_hash"] = hashlib.sha256(
        json.dumps(report["protocol"], sort_keys=True).encode()
    ).hexdigest()


def reports():
    rows = [
        dict(
            case_id=str(i),
            passed=False,
            failure="answer_mismatch",
            difficulty="easy",
            elapsed_seconds=1.0,
            tokens=10,
            sub_hits=0,
            sub_total=1,
            cost_usd=None,
            database_hash="database",
            attempted=True,
        )
        for i in range(4)
    ]
    baseline = dict(
        benchmark="dabstep-dev",
        cohort_hash="cohort",
        source_manifest_hash="sources",
        runner_hash="runner",
        model="scripted",
        model_route={"model": "scripted"},
        python="3.12",
        tool_profile="sql-rules",
        run_status="completed",
        requested_n=4,
        population_n=4,
        protocol={"version": "stage9-v1", "experience_reuse": False, "max_steps": 8},
        system_fingerprint={
            "agent_hash": "agent",
            "experience": {
                "enabled": False,
                "scope_hash": None,
                "source_hash": None,
            },
        },
        results=rows,
        summary=summarize(rows),
    )
    protocol_hash(baseline)
    candidate = copy.deepcopy(baseline)
    candidate["protocol"].update(
        experience_reuse=True,
        experience_store_hash="a" * 64,
        experience_scope_hash="sha256:" + "b" * 64,
    )
    candidate["system_fingerprint"]["experience"] = dict(
        enabled=True,
        scope_hash="sha256:" + "b" * 64,
        source_hash="c" * 64,
    )
    candidate["results"][0].update(passed=True, failure=None, sub_hits=1)
    baseline["results"][1].update(passed=True, failure=None, sub_hits=1)
    candidate["results"][2].update(failure="token_budget", tokens=20)
    baseline["summary"] = summarize(baseline["results"])
    candidate["summary"] = summarize(candidate["results"])
    protocol_hash(candidate)
    return baseline, candidate


def test_both_arms_cannot_silently_drop_the_same_task():
    baseline, candidate = reports()
    for report in (baseline, candidate):
        report["results"].pop()
        report["summary"] = summarize(report["results"])
    with pytest.raises(ValueError, match="full planned denominator"):
        compare_experience_runs(baseline, candidate)


def test_explicit_treatment_diagnostics_preserve_input_reports_and_unknown_cost():
    baseline, candidate = reports()
    before = copy.deepcopy((baseline, candidate))
    with pytest.raises(ValueError, match="incomparable protocol_hash"):
        paired_gate(baseline, candidate)
    result = compare_experience_runs(baseline, candidate)
    assert (baseline, candidate) == before
    assert result["n"] == 4
    assert result["improvements"] == ["0"]
    assert result["regressions"] == ["1"]
    assert result["pass_rate_delta"] == 0
    assert result["blocked_by"] == []
    assert result["efficiency"]["baseline"]["tokens"] == 40
    assert result["efficiency"]["candidate"]["tokens"] == 50
    assert result["efficiency"]["candidate"]["cost_usd"] is None
    assert result["failure_transitions"]["answer_mismatch->token_budget"] == 1
    assert result["statistical_inference"] == "not_run"
    assert result["repeated_pair_qualification"] == "not_run"
    assert result["production_accepted"] is False


@pytest.mark.parametrize(
    "change",
    [
        "protocol",
        "agent",
        "route",
        "database",
        "missing_database",
        "missing_row",
        "duplicate",
        "reordered",
        "hash",
        "store",
        "scope",
        "wrong_arm",
        "incomplete",
        "runner",
        "python",
        "source",
        "missing_system",
        "extra_experience_field",
    ],
)
def test_undeclared_changes_and_missing_evidence_are_refused(change):
    baseline, candidate = reports()
    if change == "protocol":
        candidate["protocol"]["max_steps"] = 9
        protocol_hash(candidate)
    elif change == "agent":
        candidate["system_fingerprint"]["agent_hash"] = "changed"
    elif change == "route":
        candidate["model_route"] = {"model": "different"}
    elif change == "database":
        candidate["results"][0]["database_hash"] = "changed"
    elif change == "missing_database":
        candidate["results"][0].pop("database_hash")
    elif change == "missing_row":
        candidate["results"].pop()
    elif change == "duplicate":
        candidate["results"][1]["case_id"] = "0"
    elif change == "reordered":
        candidate["results"].reverse()
    elif change == "hash":
        candidate["protocol_hash"] = "wrong"
    elif change == "store":
        candidate["protocol"].pop("experience_store_hash")
        protocol_hash(candidate)
    elif change == "scope":
        candidate["system_fingerprint"]["experience"]["scope_hash"] = "different"
    elif change == "wrong_arm":
        baseline["protocol"]["experience_reuse"] = True
        protocol_hash(baseline)
    elif change == "incomplete":
        candidate["run_status"] = "blocked_by_service"
    elif change == "runner":
        candidate["runner_hash"] = "different"
    elif change == "python":
        candidate["python"] = "3.13"
    elif change == "source":
        candidate["source_manifest_hash"] = "different"
    elif change == "missing_system":
        candidate.pop("system_fingerprint")
    else:
        candidate["system_fingerprint"]["experience"]["unlisted"] = True
    if change != "duplicate":
        candidate["summary"] = summarize(candidate["results"])
    with pytest.raises(ValueError):
        compare_experience_runs(baseline, candidate)


@pytest.mark.parametrize(
    "failure,attempted,severe,blocker",
    [
        ("model_error", True, False, "service_failures_confound_attribution"),
        ("answer_mismatch", False, False, "unattempted_or_unknown_tasks"),
        ("answer_mismatch", True, True, "severe_incident"),
    ],
)
def test_failures_stay_in_denominator_and_block_attribution(failure, attempted, severe, blocker):
    baseline, candidate = reports()
    candidate["results"][3].update(failure=failure, attempted=attempted, severe_incident=severe)
    candidate["summary"] = summarize(candidate["results"])
    result = compare_experience_runs(baseline, candidate)
    assert result["n"] == 4
    assert blocker in result["blocked_by"]
