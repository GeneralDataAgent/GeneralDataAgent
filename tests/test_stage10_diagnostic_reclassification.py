"""Stored report summaries can be re-derived without editing the evidence."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

RUNNER = Path(__file__).resolve().parents[1] / "evals" / "reclassify_stage10_diagnostics.py"


def runner():
    spec = importlib.util.spec_from_file_location("diagnostic_reclassify_runner", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def refused_row(**overrides):
    row = dict(
        task_id="1871",
        outcome="model_run",
        status="failed",
        passed=False,
        delegated_roles=["discovery", "data-analyst"],
        roles_complete=False,
        evidence_present=False,
        answer_matches_published=False,
        specialist_reports=[{"role": "discovery", "status": "completed"}],
        limitations=[
            "deterministic workflow refusal: annex_workflow_counterfactual_not_applicable"
        ],
        stop_diagnostic={"code": "incomplete_sequence_reason_unknown"},
    )
    row.update(overrides)
    return row


def test_a_row_is_reclassified_against_the_current_classifier():
    rows = [refused_row()]
    report = {
        "results": rows,
        # As recorded by the run, before the classifier saw the refusal.
        "summary": {"n": 10, "stop_diagnostics": {"incomplete_sequence_reason_unknown": 1}},
    }
    output = runner().reclassify(report)
    assert output["stored_stop_diagnostics"] == {"incomplete_sequence_reason_unknown": 1}
    assert output["recomputed_stop_diagnostics"] == {"workflow_refusal": 1}
    assert output["changed_rows"] == [
        {
            "task_id": "1871",
            "outcome": "model_run",
            "status": "failed",
            "stored": "incomplete_sequence_reason_unknown",
            "recomputed": "workflow_refusal",
        }
    ]
    # The planned denominator is read from the report, not from the row count.
    assert (output["n"], output["processed_n"]) == (10, 1)
    assert output["production_accepted"] is False


def test_a_row_without_a_stored_code_is_listed_not_assumed_to_agree():
    output = runner().reclassify({"results": [refused_row(stop_diagnostic=None)], "summary": {}})
    assert output["changed_rows"][0]["stored"] is None
    assert output["changed_rows"][0]["recomputed"] == "workflow_refusal"


def test_an_unchanged_row_is_not_listed():
    output = runner().reclassify(
        {"results": [refused_row(stop_diagnostic={"code": "workflow_refusal"})]}
    )
    assert output["changed_rows"] == []


def test_the_original_report_is_never_written(tmp_path, monkeypatch):
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"results": [refused_row()], "summary": {"n": 10}}))
    before = report.read_bytes()
    output = tmp_path / "reclassified.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "reclassify_stage10_diagnostics.py",
            "--report",
            str(report),
            "--output",
            str(output),
        ],
    )
    runner().main()
    assert report.read_bytes() == before
    written = json.loads(output.read_text())
    assert written["report"] == "report.json"
    assert written["new_model_calls"] == 0
    assert len(written["report_sha256"]) == 64


def test_an_existing_output_is_not_overwritten(tmp_path, monkeypatch):
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"results": [], "summary": {}}))
    output = tmp_path / "reclassified.json"
    output.write_text("prior evidence")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "reclassify_stage10_diagnostics.py",
            "--report",
            str(report),
            "--output",
            str(output),
        ],
    )
    with pytest.raises(SystemExit):
        runner().main()
    assert output.read_text() == "prior evidence"


def test_the_report_cannot_be_its_own_output(tmp_path, monkeypatch):
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"results": [], "summary": {}}))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "reclassify_stage10_diagnostics.py",
            "--report",
            str(report),
            "--output",
            str(report),
        ],
    )
    with pytest.raises(SystemExit):
        runner().main()
