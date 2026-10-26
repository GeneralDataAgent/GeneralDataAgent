"""Stored failure labels are checked against endings the current loop can produce."""

import importlib.util
import json
from pathlib import Path

import pytest

CHECKER = Path(__file__).resolve().parents[1] / "evals" / "check_stage6_failure_labels.py"


def checker():
    spec = importlib.util.spec_from_file_location("failure_label_checker", CHECKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def row(failure, *trace, case_id="1"):
    return {"case_id": case_id, "failure": failure, "trace": list(trace)}


OPEN = {"step": 3, "tool": "sql", "status": "ok"}
RAISED = {"step": 3, "status": "error", "error": "SemanticError", "code": "semantic_contract"}


def test_an_execution_error_after_a_successful_observation_is_unreachable():
    """The ending is an observation, so the loop did not end on a raised action."""
    report = {"results": [row("execution_or_model_error", RAISED, OPEN)]}
    found, unverifiable = checker().unreachable_rows(report)
    assert unverifiable == []
    (found,) = found
    assert found["case_id"] == "1"
    assert found["stored"] == "execution_or_model_error"
    assert (found["expected_terminal"], found["observed_terminal"]) == ("error", "open")
    assert found["last_status"] == "ok"


def test_an_execution_error_that_ended_on_a_raised_action_is_reachable():
    report = {"results": [row("execution_or_model_error", OPEN, RAISED)]}
    assert checker().unreachable_rows(report) == ([], [])


def test_a_step_budget_that_ended_on_a_raised_action_is_unreachable():
    """A raised final action is reported as an execution error, not a spent budget."""
    report = {"results": [row("step_budget", OPEN, RAISED)]}
    (found,), unverifiable = checker().unreachable_rows(report)
    assert unverifiable == []
    assert (found["stored"], found["observed_terminal"]) == ("step_budget", "error")


def test_a_step_budget_that_ended_on_an_observation_is_reachable():
    report = {"results": [row("step_budget", RAISED, OPEN)]}
    assert checker().unreachable_rows(report) == ([], [])


@pytest.mark.parametrize(
    "failure",
    ["answer_mismatch", "unsupported_capability", "repair_budget_exhausted", None],
)
def test_labels_decided_outside_the_loop_are_not_judged_here(failure):
    report = {"results": [row(failure, OPEN)]}
    assert checker().unreachable_rows(report) == ([], [])


def test_a_row_without_a_trace_is_reported_not_assumed_to_agree():
    report = {"results": [row("execution_or_model_error")]}
    contradicting, unverifiable = checker().unreachable_rows(report)
    assert contradicting == []
    (found,) = unverifiable
    assert found["observed_terminal"] == "unknown"


def test_an_unsupported_ending_is_not_confused_with_an_open_one():
    stopped = {"step": 4, "tool": "capability_declaration", "status": "unsupported"}
    report = {"results": [row("execution_or_model_error", stopped)]}
    (found,), _ = checker().unreachable_rows(report)
    assert found["observed_terminal"] == "stopped"


def test_the_check_records_the_report_identity_and_denominator():
    report = {
        "benchmark": "dabstep-dev",
        "summary": {"n": 10},
        "results": [row("step_budget", OPEN)],
    }
    output = checker().check(report)
    assert (output["benchmark"], output["n"], output["processed_n"]) == ("dabstep-dev", 10, 1)
    assert (output["unreachable_rows"], output["unverifiable_rows"]) == ([], [])
    assert output["production_accepted"] is False


def test_the_original_report_is_never_written(tmp_path, monkeypatch):
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"results": [row("execution_or_model_error", OPEN)]}))
    before = report.read_bytes()
    out = tmp_path / "check.json"
    monkeypatch.setattr("sys.argv", ["check", "--report", str(report), "--output", str(out)])
    checker().main()
    assert report.read_bytes() == before
    written = json.loads(out.read_text())
    assert written["report_sha256"] == __import__("hashlib").sha256(before).hexdigest()
    assert written["new_model_calls"] == 0


def test_an_existing_output_is_refused(tmp_path, monkeypatch):
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"results": []}))
    out = tmp_path / "check.json"
    out.write_text("{}")
    monkeypatch.setattr("sys.argv", ["check", "--report", str(report), "--output", str(out)])
    with pytest.raises(SystemExit):
        checker().main()
    assert out.read_text() == "{}"


def test_writing_over_the_report_is_refused(tmp_path, monkeypatch):
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"results": []}))
    monkeypatch.setattr("sys.argv", ["check", "--report", str(report), "--output", str(report)])
    with pytest.raises(SystemExit):
        checker().main()
