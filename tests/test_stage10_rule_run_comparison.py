"""Two runs of a stochastic system are compared row by row, never averaged."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

RUNNER = Path(__file__).resolve().parents[1] / "evals" / "compare_stage10_rule_runs.py"
IDENTITY = {
    "planned_task_ids": ["1", "2"],
    "dataset_hashes": {"dev.jsonl": "a"},
    "model_route": {"model": "m", "transport": "openai-chat"},
    "annex_semantics": {"null_is_wildcard": True},
}


def runner():
    spec = importlib.util.spec_from_file_location("rule_run_comparison", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def row(
    task_id,
    *,
    status="completed",
    roles=("discovery", "data-analyst"),
    passed=False,
    code="answer_mismatch",
):
    return {
        "task_id": task_id,
        "outcome": "model_run",
        "status": status,
        "delegated_roles": list(roles),
        "roles_complete": len(roles) == 2,
        "passed": passed,
        "stop_diagnostic": {"code": code},
    }


def report(rows, *, passed, n=2, **overrides):
    value = {
        "run_status": "completed",
        "summary": {
            "n": n,
            "processed_n": n,
            "pending_n": 0,
            "passed_over_all_questions": passed,
            "model_calls": 4,
            "usage": {"total_tokens": 100},
        },
        "results": rows,
        **IDENTITY,
    }
    value.update(overrides)
    return value


def test_runs_keep_separate_scores_and_list_changed_rows():
    baseline = report([row("1"), row("2", passed=True, code="completed")], passed=1)
    candidate = report(
        [row("1", roles=("discovery",)), row("2", passed=True, code="completed")], passed=1
    )
    output = runner().compare(baseline, candidate)
    # No combined rate: each run reports its own score out of its own denominator.
    assert output["scores"]["baseline"]["passed"] == 1
    assert output["scores"]["candidate"]["passed"] == 1
    assert output["scores"]["baseline"]["n"] == output["scores"]["candidate"]["n"] == 2
    assert output["paired_effect_claimed"] is False
    assert [change["task_id"] for change in output["changed_tasks"]] == ["1"]
    assert output["changed_tasks"][0]["differences"]["delegated_roles"] == [
        ["discovery", "data-analyst"],
        ["discovery"],
    ]
    assert output["stable_tasks"] == ["2"]


def test_diagnostics_hash_may_differ_but_the_measurement_may_not():
    baseline = report([row("1"), row("2")], passed=0, diagnostics_hash="old")
    candidate = report([row("1"), row("2")], passed=0, diagnostics_hash="new")
    output = runner().compare(baseline, candidate)
    recorded = output["identity"]["recorded"]["diagnostics_hash"]
    assert recorded == {"baseline": "old", "candidate": "new", "same": False}
    assert output["changed_tasks"] == []


@pytest.mark.parametrize(
    "field", ["planned_task_ids", "dataset_hashes", "model_route", "annex_semantics"]
)
def test_a_different_measurement_is_refused(field):
    baseline = report([row("1"), row("2")], passed=0)
    candidate = report([row("1"), row("2")], passed=0, **{field: "different"})
    # One module instance per call: a second import would define a different
    # NotComparable class, so the raises() guard would not catch it.
    module = runner()
    with pytest.raises(module.NotComparable, match=field):
        module.compare(baseline, candidate)


def test_an_incomplete_run_is_refused():
    baseline = report([row("1"), row("2")], passed=0)
    short = report([row("1")], passed=0)
    short["summary"].update(processed_n=1, pending_n=1)
    module = runner()
    with pytest.raises(module.NotComparable, match="denominator"):
        module.compare(baseline, short)
    interrupted = report([row("1"), row("2")], passed=0, run_status="blocked_by_service")
    with pytest.raises(module.NotComparable, match="did not complete"):
        module.compare(baseline, interrupted)


def test_different_task_sets_are_refused():
    baseline = report([row("1"), row("2")], passed=0)
    candidate = report([row("1"), row("3")], passed=0)
    module = runner()
    with pytest.raises(module.NotComparable, match="different task ids"):
        module.compare(baseline, candidate)


def test_the_comparison_never_writes_the_reports(tmp_path, monkeypatch):
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    baseline.write_text(json.dumps(report([row("1"), row("2")], passed=0)))
    candidate.write_text(json.dumps(report([row("1"), row("2")], passed=0)))
    before = baseline.read_bytes(), candidate.read_bytes()
    output = tmp_path / "comparison.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "compare_stage10_rule_runs.py",
            "--baseline",
            str(baseline),
            "--candidate",
            str(candidate),
            "--output",
            str(output),
        ],
    )
    runner().main()
    assert (baseline.read_bytes(), candidate.read_bytes()) == before
    written = json.loads(output.read_text())
    assert written["new_model_calls"] == 0
    assert set(written["report_sha256"]) == {"baseline", "candidate"}


def test_an_existing_output_or_a_report_as_output_is_refused(tmp_path, monkeypatch):
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    baseline.write_text(json.dumps(report([row("1"), row("2")], passed=0)))
    candidate.write_text(json.dumps(report([row("1"), row("2")], passed=0)))
    existing = tmp_path / "existing.json"
    existing.write_text("prior evidence")
    for output in (existing, baseline):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "compare_stage10_rule_runs.py",
                "--baseline",
                str(baseline),
                "--candidate",
                str(candidate),
                "--output",
                str(output),
            ],
        )
        with pytest.raises(SystemExit):
            runner().main()
    assert existing.read_text() == "prior evidence"
    assert json.loads(baseline.read_text())["run_status"] == "completed"
