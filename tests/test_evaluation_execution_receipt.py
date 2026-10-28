"""Fresh evaluation receipts bind observed steps; legacy reports stay untouched."""

import copy
import hashlib
import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.contracts import ActorContext, ExecutionReceipt
from generaldataagent.evaluation.receipts import (
    EvaluationReceiptError,
    EvaluationReceiptRecorder,
    validate_evaluation_receipts,
)
from generaldataagent.evaluation.stage_seven_acceptance import answer_receipt_errors
from generaldataagent.evaluation.stage_six import BoundedSQLAgent, Task

ACTOR = ActorContext(
    user_id="researcher", tenant_id="local", project_id="eval", data_domains=("test",)
)


@pytest.fixture
def setup(tmp_path):
    database = tmp_path / "data.sqlite"
    with sqlite3.connect(database) as db:
        db.executescript("CREATE TABLE data(value INT); INSERT INTO data VALUES (2),(4);")
    task = Task("infiagent-dabench", "synthetic", "Mean value?", "@mean[value]", "easy", "data")
    store = LocalArtifactStore(tmp_path / "artifacts")
    return database, task, store


def recorder(setup):
    database, task, store = setup
    return EvaluationReceiptRecorder(
        artifacts=store,
        actor=ACTOR,
        task_id=uuid4(),
        trace_id="test-run",
        task=task,
        database=database,
        documents="",
        profile="sql-semantic",
    )


def run(setup, rec=None, *, bird=False):
    database, task, _ = setup
    if bird:
        task = replace(task, benchmark="bird-mini-dev-sqlite", instructions="")
    actions = iter(
        [{"submit_sql": {"sql": "SELECT AVG(value) AS mean FROM data", "output_columns": ["mean"]}}]
        if bird
        else [
            {"sql": "SELECT AVG(value) AS mean FROM data"},
            {"submit_answers": {"fields": [{"name": "mean", "step": 1, "path": ["rows", 0, 0]}]}},
        ]
    )

    class Model:
        def invoke(self, messages):
            return SimpleNamespace(
                content=json.dumps(next(actions)), usage_metadata={"total_tokens": 5}
            )

    return BoundedSQLAgent(Model(), tool_profile="sql-semantic", max_steps=2).run(
        task,
        database,
        receipt_recorder=rec,
    )


def test_worker_records_receipts_from_explicit_payload(setup, monkeypatch, capsys):
    import io
    from dataclasses import asdict

    from generaldataagent.evaluation import stage_six_worker as worker

    class Model:
        def invoke(self, messages):
            return SimpleNamespace(
                content=json.dumps({"sql": "SELECT AVG(value) AS mean FROM data"}),
                usage_metadata={"total_tokens": 2},
            )

    monkeypatch.setattr(worker, "resolve_evaluation_model", lambda **kwargs: (object(), {}))
    monkeypatch.setattr(worker, "build_evaluation_model", lambda *args: Model())
    task_id = uuid4()
    payload = {
        "app_type": "scripted",
        "provider_id": "scripted",
        "task": asdict(setup[1]),
        "database": str(setup[0]),
        "documents": "",
        "tool_profile": "sql-semantic",
        "max_steps": 1,
        "receipt_config": {
            "artifact_root": str(setup[2].root),
            "actor": ACTOR.model_dump(mode="json"),
            "task_id": str(task_id),
            "trace_id": "worker-test",
        },
    }
    monkeypatch.setattr(worker.sys, "stdin", io.StringIO(json.dumps(payload)))
    worker.main()
    result = json.loads(capsys.readouterr().out)
    (receipt,) = validate_evaluation_receipts(result, read_bytes=setup[2].read_bytes)
    assert receipt.task_id == str(task_id)
    assert receipt.trace_id == "worker-test"
    assert result["failure"] == "step_budget"


def test_fresh_step_receipt_round_trip_preserves_legacy_hash_and_answer_binding(setup):
    rec = recorder(setup)
    result = run(setup, rec)
    assert result["failure"] is None and result["answers"] == {"mean": 3.0}
    (receipt,) = validate_evaluation_receipts(result, read_bytes=setup[2].read_bytes)
    assert isinstance(receipt, ExecutionReceipt)
    assert receipt.task_id == str(rec.task_id) and receipt.trace_id == "test-run"
    assert receipt.actor_id == ACTOR.user_id
    assert receipt.method.method_id == "evaluation.sql"
    assert receipt.verification.scope == "producer_self_check"
    assert receipt.usage.tool_calls == 1
    assert receipt.usage.input_tokens is None and receipt.usage.total_tokens is None
    assert receipt.usage.telemetry_complete is False
    binding = result["answer_evidence"]["mean"]
    assert receipt.result_refs[0].content_hash == "sha256:" + binding["result_hash"]
    assert result["trace"][0]["result_hash"] == binding["result_hash"]
    assert not answer_receipt_errors(
        {"benchmark": "infiagent-dabench", "results": [dict(result, case_id="x", passed=True)]},
        read_bytes=setup[2].read_bytes,
    )
    assert {s.source_id for s in receipt.sources} == {"database", "documents", "public_task"}


def test_disabled_and_legacy_rows_do_not_invent_receipts_or_mutate(setup):
    legacy = run(setup)
    before = copy.deepcopy(legacy)
    assert "execution_receipts" not in legacy and "receipt_context" not in legacy
    assert validate_evaluation_receipts(legacy) == ()
    assert legacy == before


@pytest.mark.parametrize(
    "change",
    [
        "task",
        "actor",
        "source",
        "runtime",
        "method",
        "result",
        "cross_task",
        "strength",
        "step",
        "duplicate",
        "missing",
        "trace",
        "binding",
        "raw",
        "database",
        "envelope",
    ],
)
def test_mismatched_fresh_evidence_fails_closed(setup, change):
    result = run(setup, recorder(setup))
    entry = result["execution_receipts"][0]
    receipt = entry["receipt"]
    if change == "task":
        receipt["task_id"] = str(uuid4())
    elif change == "actor":
        receipt["actor_id"] = "other"
    elif change == "source":
        receipt["sources"][0]["content_hash"] = "sha256:" + "a" * 64
    elif change == "runtime":
        receipt["method"]["runtime_fingerprint"] = "sha256:" + "a" * 64
    elif change == "method":
        receipt["method"]["method_id"] = "evaluation.fixed_analysis"
    elif change == "result":
        entry["result_hash"] = "a" * 64
    elif change == "cross_task":
        receipt["result_refs"][0]["task_id"] = str(uuid4())
    elif change == "strength":
        receipt["verification"]["scope"] = "authoritative_rederivation"
    elif change == "step":
        entry["step"] = True
    elif change == "duplicate":
        result["execution_receipts"].append(copy.deepcopy(entry))
    elif change == "missing":
        result["execution_receipts"] = []
    elif change == "trace":
        result["trace"][0]["result_hash"] = "b" * 64
    elif change == "binding":
        result["answer_evidence"]["mean"]["method_id"] = "describe"
    elif change == "raw":
        result["answer_evidence"]["mean"]["raw_value"] = 999
    elif change == "database":
        result["database_hash"] = "c" * 64
    else:
        del result["receipt_context"]
    with pytest.raises((ValueError, KeyError, TypeError)):
        validate_evaluation_receipts(result, read_bytes=setup[2].read_bytes)


@pytest.mark.parametrize("failure", ["substitution", "cross_task", "corruption", "read", "write"])
def test_storage_failure_stops_without_model_repair(setup, monkeypatch, failure):
    rec = recorder(setup)
    store = setup[2]
    original = store.put_bytes

    def put(**kwargs):
        if failure == "write":
            raise OSError("synthetic write")
        if failure == "substitution":
            kwargs["content"] = b'{"wrong": true}'
        if failure == "cross_task":
            kwargs["task_id"] = uuid4()
        ref = original(**kwargs)
        if failure == "corruption":
            (store.root / ref.uri.removeprefix("artifact://local/")).write_bytes(b"bad")
        return ref

    monkeypatch.setattr(store, "put_bytes", put)
    if failure == "read":
        monkeypatch.setattr(store, "read_bytes", lambda ref: (_ for _ in ()).throw(OSError("read")))
    with pytest.raises(EvaluationReceiptError):
        run(setup, rec)
    assert rec.steps == []


def test_changed_source_before_export_fails_and_old_artifact_bytes_remain(setup):
    rec = recorder(setup)
    result = run(setup, rec)
    ref = ExecutionReceipt.model_validate(result["execution_receipts"][0]["receipt"]).result_refs[0]
    before = setup[2].read_bytes(ref)
    with sqlite3.connect(setup[0]) as db:
        db.execute("INSERT INTO data VALUES (10)")
    with pytest.raises(EvaluationReceiptError, match="database_changed"):
        rec.export()
    assert setup[2].read_bytes(ref) == before


def test_qualification_does_not_ignore_malformed_receipts_on_failed_rows(setup):
    result = run(setup, recorder(setup))
    result["execution_receipts"] = None
    report = {
        "benchmark": "bird-mini-dev-sqlite",
        "results": [dict(result, case_id="x", passed=False)],
    }
    assert answer_receipt_errors(report) == ["x:invalid_execution_receipt"]


@pytest.mark.parametrize("change", ["task", "database", "documents", "profile"])
def test_recorder_requires_exact_run_inputs_before_model_access(setup, change, tmp_path):
    rec = recorder(setup)
    database, task, _ = setup
    documents, profile = "", "sql-semantic"
    if change == "task":
        task = replace(task, question="Different question")
    elif change == "database":
        other = tmp_path / "other.sqlite"
        other.write_bytes(database.read_bytes())
        database = other
    elif change == "documents":
        documents = "Different text"
    else:
        profile = "sql"
    with pytest.raises(EvaluationReceiptError, match="inputs_mismatch"):
        BoundedSQLAgent(object(), tool_profile=profile).run(
            task, database, documents=documents, receipt_recorder=rec
        )
    assert rec.steps == []


def test_wal_only_update_cannot_reuse_main_file_digest(setup):
    rec = recorder(setup)
    original = setup[0].read_bytes()
    with sqlite3.connect(setup[0]) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # Bind the checkpointed main file before the committed WAL change.
        rec = recorder(setup)
        unchanged = setup[0].read_bytes()
        writer.execute("UPDATE data SET value=40")
        writer.commit()
        assert setup[0].read_bytes() == unchanged
        with pytest.raises(EvaluationReceiptError, match="active_wal"):
            run(setup, rec)
    assert original != b""


def test_relabelled_method_cannot_override_persisted_result(setup):
    result = run(setup, recorder(setup))
    entry = result["execution_receipts"][0]
    entry["tool"] = "fixed_analysis"
    entry["method_id"] = "invented_method"
    entry["receipt"]["method"]["method_id"] = "evaluation.fixed_analysis"
    result["trace"][0].update(
        tool="fixed_analysis", action={"analysis": {"operation": "invented_method"}}
    )
    result["answer_evidence"]["mean"]["method_id"] = "invented_method"
    with pytest.raises(ValueError, match="method_mismatch"):
        validate_evaluation_receipts(result, read_bytes=setup[2].read_bytes)


def test_fresh_qualification_requires_available_artifact_reader(setup):
    result = run(setup, recorder(setup))
    report = {"benchmark": "infiagent-dabench", "results": [dict(result, case_id="x", passed=True)]}
    assert answer_receipt_errors(report) == ["x:invalid_execution_receipt"]
    assert answer_receipt_errors(report, read_bytes=setup[2].read_bytes) == []
    ref = result["execution_receipts"][0]["receipt"]["result_refs"][0]
    (setup[2].root / ref["uri"].removeprefix("artifact://local/")).unlink()
    assert answer_receipt_errors(report, read_bytes=setup[2].read_bytes) == [
        "x:invalid_execution_receipt"
    ]


def test_readback_detects_changed_artifact_bytes(setup):
    result = run(setup, recorder(setup))
    ref = result["execution_receipts"][0]["receipt"]["result_refs"][0]
    path = setup[2].root / ref["uri"].removeprefix("artifact://local/")
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash mismatch"):
        validate_evaluation_receipts(result, read_bytes=setup[2].read_bytes)
    assert hashlib.sha256(path.read_bytes()).hexdigest() != ref["content_hash"].removeprefix(
        "sha256:"
    )
