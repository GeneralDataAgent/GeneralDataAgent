"""The readback runner reports stored evidence only and never rewrites a report."""

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.contracts import ActorContext
from generaldataagent.evaluation.receipts import EvaluationReceiptRecorder
from generaldataagent.evaluation.stage_six import BoundedSQLAgent, Task

RUNNER = Path(__file__).resolve().parents[1] / "evals" / "verify_evaluation_receipts.py"
ACTOR = ActorContext(
    user_id="researcher", tenant_id="local", project_id="eval", data_domains=("test",)
)


def runner():
    spec = importlib.util.spec_from_file_location("receipt_readback_runner", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_case(tmp_path, *, receipts: bool):
    database = tmp_path / "data.sqlite"
    with sqlite3.connect(database) as db:
        db.executescript("CREATE TABLE data(value INT); INSERT INTO data VALUES (2),(4);")
    task = Task("infiagent-dabench", "synthetic", "Mean value?", "@mean[value]", "easy", "data")
    store = LocalArtifactStore(tmp_path / "artifacts")
    recorder = (
        EvaluationReceiptRecorder(
            artifacts=store,
            actor=ACTOR,
            task_id=uuid4(),
            trace_id="readback-run",
            task=task,
            database=database,
            documents="",
            profile="sql-semantic",
        )
        if receipts
        else None
    )

    actions = iter(
        [
            {"sql": "SELECT AVG(value) AS mean FROM data"},
            {"submit_answers": {"fields": [{"name": "mean", "step": 1, "path": ["rows", 0, 0]}]}},
        ]
    )

    class Model:
        def invoke(self, messages):
            return SimpleNamespace(
                content=json.dumps(next(actions)),
                usage_metadata={"total_tokens": 2},
            )

    result = BoundedSQLAgent(Model(), tool_profile="sql-semantic", max_steps=2).run(
        task, database, receipt_recorder=recorder
    )
    assert result["failure"] is None and result["answers"] == {"mean": 3.0}
    row = dict(result, case_id="case-1", passed=True)
    report = {"benchmark": "infiagent-dabench", "results": [row]}
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report))
    return path, store.root, report


def invoke(module, monkeypatch, report: Path, artifact_root: Path, output: Path):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verify_evaluation_receipts.py",
            "--report",
            str(report),
            "--artifact-root",
            str(artifact_root),
            "--output",
            str(output),
        ],
    )
    module.main()
    return json.loads(output.read_text())


def test_readback_verifies_fresh_receipts(tmp_path, monkeypatch):
    report, artifacts, original = make_case(tmp_path, receipts=True)
    summary = invoke(runner(), monkeypatch, report, artifacts, tmp_path / "readback.json")
    assert summary["n"] == 1
    assert summary["step_receipts"] == 1
    assert summary["receipt_errors"] == []
    assert summary["stage7_errors"] == []
    assert summary["new_model_calls"] == 0
    assert summary["production_accepted"] is False
    assert json.loads(report.read_text()) == original


def test_legacy_rows_stay_readable_without_receipts(tmp_path, monkeypatch):
    report, artifacts, _ = make_case(tmp_path, receipts=False)
    summary = invoke(runner(), monkeypatch, report, artifacts, tmp_path / "readback.json")
    assert summary["step_receipts"] == 0
    assert summary["receipt_errors"] == []
    assert summary["stage7_errors"] == []


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_unreadable_artifacts_block_readback(tmp_path, monkeypatch, damage):
    report, artifacts, _ = make_case(tmp_path, receipts=True)
    row = json.loads(report.read_text())["results"][0]
    ref = row["execution_receipts"][0]["receipt"]["result_refs"][0]
    path = artifacts / ref["uri"].removeprefix("artifact://local/")
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"changed")
    summary = invoke(runner(), monkeypatch, report, artifacts, tmp_path / "readback.json")
    assert summary["receipt_errors"] == ["case-1:invalid_execution_receipt"]
    assert "receipt_readback_incomplete" in summary["stage7_errors"]


def test_existing_output_is_never_overwritten(tmp_path, monkeypatch):
    report, artifacts, _ = make_case(tmp_path, receipts=True)
    output = tmp_path / "readback.json"
    output.write_text("prior evidence")
    with pytest.raises(SystemExit):
        invoke(runner(), monkeypatch, report, artifacts, output)
    assert output.read_text() == "prior evidence"
