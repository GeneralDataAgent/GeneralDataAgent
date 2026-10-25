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
            usage_status="complete",
            sub_hits=0,
            sub_total=1,
            cost_usd=None,
            database_hash="database",
            attempted=True,
            experience_used=[],
            trace=[],
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
    with pytest.raises(ValueError, match="incomparable protocol_hash"):
        paired_gate(baseline, candidate)
    candidate["results"][0]["experience_used"] = [
        {"step": 1, "failure_code": "answer_mismatch", "tool": "sql", "n": 1, "dropped": 0}
    ]
    candidate["summary"] = summarize(candidate["results"])
    before = copy.deepcopy((baseline, candidate))
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


@pytest.mark.parametrize("arm", ["baseline", "candidate"])
@pytest.mark.parametrize("usage_status", [None, "partial_or_unknown", "unknown_worker_failure"])
def test_incomplete_usage_blocks_resource_evaluation_without_dropping_results(arm, usage_status):
    baseline, candidate = reports()
    report = baseline if arm == "baseline" else candidate
    row = report["results"][3]
    if usage_status is None:
        row.pop("usage_status")
    else:
        row["usage_status"] = usage_status
    report["summary"] = summarize(report["results"])
    before = copy.deepcopy((baseline, candidate))
    result = compare_experience_runs(baseline, candidate)
    # The fixture delivers no hints, so delivery is independently blocked; the
    # usage evidence must still be reported rather than dropped.
    assert "incomplete_token_telemetry" in result["blocked_by"]
    assert result["n"] == 4
    assert result["improvements"] == ["0"]
    assert result["regressions"] == ["1"]
    assert result["efficiency"][arm]["token_telemetry_complete"] is False
    assert result["efficiency"][arm]["tokens"] == report["summary"]["tokens"]
    assert result["efficiency"][arm]["cost_usd"] is None
    assert (baseline, candidate) == before


@pytest.mark.parametrize("arm", ["baseline", "candidate"])
@pytest.mark.parametrize("field,value", [("sub_total", 2), ("difficulty", "hard")])
def test_task_scoring_metadata_must_match_between_arms(arm, field, value):
    baseline, candidate = reports()
    report = baseline if arm == "baseline" else candidate
    report["results"][3][field] = value
    report["summary"] = summarize(report["results"])
    before = copy.deepcopy((baseline, candidate))
    with pytest.raises(ValueError, match="experience task scoring metadata changed"):
        compare_experience_runs(baseline, candidate)
    assert (baseline, candidate) == before


def test_equal_run_totals_cannot_hide_changed_per_task_denominators():
    baseline, candidate = reports()
    for report in (baseline, candidate):
        report["results"][2]["sub_total"] = 2
    candidate["results"][2]["sub_total"] = 1
    candidate["results"][3]["sub_total"] = 2
    for report in (baseline, candidate):
        report["summary"] = summarize(report["results"])
    assert sum(row["sub_total"] for row in baseline["results"]) == sum(
        row["sub_total"] for row in candidate["results"]
    )
    with pytest.raises(ValueError, match="experience task scoring metadata changed"):
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


def test_treatment_that_never_reaches_a_task_is_reported_not_scored():
    baseline, candidate = reports()
    result = compare_experience_runs(baseline, candidate)
    assert result["experience_delivery"]["candidate_cases_with_hints"] == 0
    assert result["experience_delivery"]["baseline_cases_with_hints"] == 0
    assert "experience_treatment_not_delivered" in result["blocked_by"]
    # Observed outcomes are still reported; they are simply not attributable.
    assert result["improvements"] == ["0"]
    assert result["regressions"] == ["1"]


@pytest.mark.parametrize("delivered", [False, True])
def test_unavailable_experience_blocks_attribution_even_with_partial_delivery(delivered):
    baseline, candidate = reports()
    candidate["results"][2]["trace"] = [
        {"step": 2, "status": "experience_unavailable", "error": "OperationalError"}
    ]
    if delivered:
        candidate["results"][0]["experience_used"] = [
            {"step": 1, "failure_code": "answer_mismatch", "tool": "sql", "n": 1, "dropped": 0}
        ]
    candidate["summary"] = summarize(candidate["results"])
    before = copy.deepcopy((baseline, candidate))
    result = compare_experience_runs(baseline, candidate)
    assert "experience_unavailable_confound_attribution" in result["blocked_by"]
    assert ("experience_treatment_not_delivered" in result["blocked_by"]) is not delivered
    assert result["experience_delivery"]["candidate_cases_with_hints"] == int(delivered)
    assert result["n"] == 4
    assert result["improvements"] == ["0"]
    assert result["regressions"] == ["1"]
    assert result["efficiency"]["candidate"]["tokens"] == 50
    assert result["efficiency"]["candidate"]["cost_usd"] is None
    assert result["production_accepted"] is False
    assert (baseline, candidate) == before


@pytest.mark.parametrize("n,dropped", [(1, 0), (0, 2), (1, 2)])
def test_same_step_cannot_record_both_outage_and_hint_processing(n, dropped):
    baseline, candidate = reports()
    row = candidate["results"][0]
    row["trace"] = [{"step": 2, "status": "experience_unavailable", "error": "OSError"}]
    row["experience_used"] = [
        {"step": 2, "failure_code": "answer_mismatch", "tool": "sql", "n": n, "dropped": dropped}
    ]
    candidate["summary"] = summarize(candidate["results"])
    before = copy.deepcopy((baseline, candidate))
    with pytest.raises(ValueError, match="experience delivery conflicts with unavailable step"):
        compare_experience_runs(baseline, candidate)
    assert (baseline, candidate) == before


@pytest.mark.parametrize("same_case", [False, True])
def test_outage_and_hint_processing_can_occur_in_separate_calls(same_case):
    baseline, candidate = reports()
    candidate["results"][0]["trace"] = [
        {"step": 2, "status": "experience_unavailable", "error": "OSError"}
    ]
    row = candidate["results"][0 if same_case else 1]
    row["experience_used"] = [
        {
            "step": 3 if same_case else 2,
            "failure_code": "answer_mismatch",
            "tool": "sql",
            "n": 1,
            "dropped": 2,
        }
    ]
    candidate["summary"] = summarize(candidate["results"])
    before = copy.deepcopy((baseline, candidate))
    result = compare_experience_runs(baseline, candidate)
    assert result["blocked_by"] == ["experience_unavailable_confound_attribution"]
    assert result["experience_delivery"]["candidate_hints"] == 1
    assert result["experience_delivery"]["candidate_hints_dropped"] == 2
    assert result["n"] == 4
    assert result["efficiency"]["candidate"]["tokens"] == 50
    assert result["production_accepted"] is False
    assert (baseline, candidate) == before


@pytest.mark.parametrize("arm", ["baseline", "candidate"])
def test_missing_trace_field_is_refused_even_when_summary_matches(arm):
    baseline, candidate = reports()
    report = baseline if arm == "baseline" else candidate
    report["results"][0].pop("trace")
    report["summary"] = summarize(report["results"])
    before = copy.deepcopy((baseline, candidate))
    with pytest.raises(ValueError, match="experience trace evidence invalid"):
        compare_experience_runs(baseline, candidate)
    assert (baseline, candidate) == before


@pytest.mark.parametrize("arm", ["baseline", "candidate"])
@pytest.mark.parametrize("trace", [None, {}, "trace", [None], [{"step": 1}], [{"status": 1}]])
def test_malformed_trace_evidence_is_refused(arm, trace):
    baseline, candidate = reports()
    report = baseline if arm == "baseline" else candidate
    report["results"][0]["trace"] = trace
    report["summary"] = summarize(report["results"])
    before = copy.deepcopy((baseline, candidate))
    with pytest.raises(ValueError, match="experience trace evidence invalid"):
        compare_experience_runs(baseline, candidate)
    assert (baseline, candidate) == before


def test_off_arm_experience_unavailable_is_refused():
    baseline, candidate = reports()
    baseline["results"][0]["trace"] = [
        {"step": 1, "status": "experience_unavailable", "error": "OperationalError"}
    ]
    baseline["summary"] = summarize(baseline["results"])
    with pytest.raises(ValueError, match="off arm recorded experience processing"):
        compare_experience_runs(baseline, candidate)


def test_hints_in_the_off_arm_are_refused():
    baseline, candidate = reports()
    baseline["results"][0]["experience_used"] = [
        {"step": 1, "failure_code": "answer_mismatch", "tool": "sql", "n": 1, "dropped": 0}
    ]
    baseline["summary"] = summarize(baseline["results"])
    with pytest.raises(ValueError, match="off arm received experience"):
        compare_experience_runs(baseline, candidate)


def test_budget_drops_in_the_off_arm_are_refused():
    baseline, candidate = reports()
    baseline["results"][0]["experience_used"] = [
        {"step": 1, "failure_code": "answer_mismatch", "tool": "sql", "n": 0, "dropped": 2}
    ]
    baseline["summary"] = summarize(baseline["results"])
    before = copy.deepcopy((baseline, candidate))
    with pytest.raises(ValueError, match="off arm contains experience budget drops"):
        compare_experience_runs(baseline, candidate)
    assert (baseline, candidate) == before


def test_budget_drops_in_the_on_arm_remain_reported_without_delivery():
    baseline, candidate = reports()
    candidate["results"][0]["experience_used"] = [
        {"step": 1, "failure_code": "answer_mismatch", "tool": "sql", "n": 0, "dropped": 2}
    ]
    candidate["summary"] = summarize(candidate["results"])
    before = copy.deepcopy((baseline, candidate))
    result = compare_experience_runs(baseline, candidate)
    assert result["experience_delivery"] == {
        "baseline_cases_with_hints": 0,
        "candidate_cases_with_hints": 0,
        "candidate_hints": 0,
        "candidate_hints_dropped": 2,
        "by_failure_code": {},
    }
    assert result["blocked_by"] == ["experience_treatment_not_delivered"]
    assert result["n"] == 4
    assert (baseline, candidate) == before


def test_on_arm_hint_delivery_is_counted_from_case_evidence():
    baseline, candidate = reports()
    candidate["results"][0]["experience_used"] = [
        {"step": 1, "failure_code": "answer_mismatch", "tool": "sql", "n": 2, "dropped": 0}
    ]
    candidate["summary"] = summarize(candidate["results"])
    result = compare_experience_runs(baseline, candidate)
    assert result["experience_delivery"] == {
        "baseline_cases_with_hints": 0,
        "candidate_cases_with_hints": 1,
        "candidate_hints": 2,
        "candidate_hints_dropped": 0,
        "by_failure_code": {"answer_mismatch": 2},
    }
    assert "experience_treatment_not_delivered" not in result["blocked_by"]


@pytest.mark.parametrize("steps", [(1, 1), (2, 1), (1, 3, 2)])
def test_duplicate_or_reordered_hint_steps_are_refused(steps):
    baseline, candidate = reports()
    candidate["results"][0]["experience_used"] = [
        {"step": step, "failure_code": "answer_mismatch", "tool": "sql", "n": 1, "dropped": 0}
        for step in steps
    ]
    candidate["summary"] = summarize(candidate["results"])
    before = copy.deepcopy((baseline, candidate))
    with pytest.raises(ValueError, match="experience delivery steps must strictly increase"):
        compare_experience_runs(baseline, candidate)
    assert (baseline, candidate) == before


def test_hint_steps_can_skip_and_restart_for_each_case():
    baseline, candidate = reports()
    for row in candidate["results"][:2]:
        row["experience_used"] = [
            {"step": 1, "failure_code": "answer_mismatch", "tool": "sql", "n": 1, "dropped": 0},
            {"step": 3, "failure_code": "answer_mismatch", "tool": "sql", "n": 0, "dropped": 2},
        ]
    candidate["summary"] = summarize(candidate["results"])
    result = compare_experience_runs(baseline, candidate)
    assert result["experience_delivery"] == {
        "baseline_cases_with_hints": 0,
        "candidate_cases_with_hints": 2,
        "candidate_hints": 2,
        "candidate_hints_dropped": 4,
        "by_failure_code": {"answer_mismatch": 2},
    }
    assert result["n"] == 4
    assert result["production_accepted"] is False


def test_missing_or_malformed_hint_evidence_is_refused():
    baseline, candidate = reports()
    candidate["results"][1].pop("experience_used")
    candidate["summary"] = summarize(candidate["results"])
    with pytest.raises(ValueError, match="experience delivery evidence missing"):
        compare_experience_runs(baseline, candidate)


@pytest.mark.parametrize(
    "hint",
    [
        {"step": 1, "failure_code": "answer_mismatch", "tool": "sql", "n": 0, "dropped": 0},
        {"step": 1, "failure_code": "", "tool": "sql", "n": 1, "dropped": 0},
        {"step": 1, "failure_code": "answer_mismatch", "tool": "sql", "n": True, "dropped": 0},
        {"step": 1, "failure_code": "answer_mismatch", "tool": "sql", "n": 1, "dropped": 0, "x": 1},
        {"step": "1", "failure_code": "answer_mismatch", "tool": "sql", "n": 1, "dropped": 0},
        {"step": 1, "failure_code": "answer_mismatch", "tool": "sql", "n": 1, "dropped": -1},
        {"step": 1, "failure_code": "answer_mismatch", "tool": "sql", "n": 0, "dropped": True},
    ],
)
def test_malformed_hint_records_are_refused(hint):
    baseline, candidate = reports()
    candidate["results"][2]["experience_used"] = [hint]
    candidate["summary"] = summarize(candidate["results"])
    with pytest.raises(ValueError, match="experience delivery evidence invalid"):
        compare_experience_runs(baseline, candidate)
