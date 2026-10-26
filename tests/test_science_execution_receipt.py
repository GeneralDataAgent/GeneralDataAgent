"""Science receipts describe observed execution without upgrading its self-checks."""

import hashlib
import json
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_data_science import _run, _snapshot, _spec

from generaldataagent.contracts import ExecutionMethod, ExperimentReport, ExperimentSpec
from generaldataagent.data_science.tracking import SQLiteExperimentTracker
from generaldataagent.data_science.workbench import DataScienceWorkbench


def test_science_receipt_binds_input_configuration_identity_and_actual_runtime(tmp_path):
    task, store, snapshot = _snapshot(tmp_path)
    spec = _spec(task, snapshot.snapshot_id).model_copy(update={"random_seed": 17})
    workbench = DataScienceWorkbench(artifact_store=store)
    report = _run(workbench, snapshot, spec)
    receipt = report.receipt
    assert receipt is not None
    assert receipt.task_id == str(task)
    assert (receipt.actor_id, receipt.tenant_id, receipt.project_id) == (
        "scientist",
        "tenant-a",
        "project-a",
    )
    assert receipt.trace_id == f"experiment-{spec.experiment_id}"
    assert receipt.action == "python.experiment"
    assert receipt.method.method_id == "science.baseline"
    assert receipt.method.method_version == report.template_version == "1.0"
    assert receipt.method.runtime_fingerprint == workbench.sandbox.runtime_fingerprint
    assert receipt.method.random_seed == 17
    expected_spec = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(spec.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    assert {source.source_id: source.content_hash for source in receipt.sources} == {
        "snapshot": snapshot.content_hash,
        "experiment_spec": expected_spec,
    }
    assert receipt.result_refs == (report.output_artifact,)
    assert store.read_bytes(receipt.result_refs[0])
    assert receipt.completeness == "complete"
    assert receipt.verification.scope == "producer_self_check"
    assert receipt.verification.passed == report.passed
    assert receipt.verification.check_codes == tuple(check.code for check in report.checks)
    assert receipt.usage.total_tokens is None and receipt.usage.tool_calls is None
    assert receipt.usage.telemetry_complete is False
    assert report.schema_version == "1.1"


def test_warning_failure_does_not_change_error_gate_semantics(tmp_path):
    task, store, snapshot = _snapshot(tmp_path)
    spec = _spec(task, snapshot.snapshot_id).model_copy(
        update={"subgroup_columns": ("customer_group",), "minimum_subgroup_rows": 10}
    )
    report = _run(DataScienceWorkbench(artifact_store=store), snapshot, spec)
    assert any(not check.passed and check.severity == "warning" for check in report.checks)
    assert report.passed and report.receipt.verification.passed
    assert "subgroup_too_small" in report.receipt.verification.check_codes


@pytest.mark.parametrize("fault", ["corrupt", "cross_task", "read_error", "wrong_content"])
def test_result_storage_faults_prevent_receipt_return_and_tracking(tmp_path, monkeypatch, fault):
    task, store, snapshot = _snapshot(tmp_path)
    tracker = SQLiteExperimentTracker(tmp_path / "tracker.sqlite")
    original = store.put_json

    def faulty(**arguments):
        if fault == "cross_task":
            arguments = {**arguments, "task_id": uuid4()}
        if fault == "wrong_content":
            changed = json.loads(json.dumps(arguments["value"]))
            changed["metrics"]["accuracy"] = 0.123456
            arguments = {**arguments, "value": changed}
        ref = original(**arguments)
        if fault == "corrupt":
            (store.root / ref.uri.removeprefix("artifact://local/")).write_bytes(b"corrupt")
        return ref

    monkeypatch.setattr(store, "put_json", faulty)
    if fault == "read_error":
        original_read = store.read_bytes

        def fail_result(ref):
            if ref.artifact_type == "experiment_result":
                raise OSError("synthetic result read outage")
            return original_read(ref)

        monkeypatch.setattr(store, "read_bytes", fail_result)
    with pytest.raises((ValueError, OSError)):
        _run(
            DataScienceWorkbench(artifact_store=store, tracker=tracker),
            snapshot,
            _spec(task, snapshot.snapshot_id),
        )
    assert tracker.list_for_snapshot(snapshot.snapshot_id) == ()


def test_tracker_restart_preserves_receipt_and_old_records_remain_readable(tmp_path):
    import sqlite3

    task, store, snapshot = _snapshot(tmp_path)
    path = tmp_path / "tracker.sqlite"
    tracker = SQLiteExperimentTracker(path)
    spec = _spec(task, snapshot.snapshot_id)
    report = _run(DataScienceWorkbench(artifact_store=store, tracker=tracker), snapshot, spec)
    assert SQLiteExperimentTracker(path).get(spec.experiment_id).report == report
    with sqlite3.connect(path) as db:
        content = db.execute("SELECT record_json FROM experiments").fetchone()[0]
        legacy = json.loads(content)
        legacy["report"].pop("receipt")
        legacy["report"]["schema_version"] = "1.0"
        encoded = json.dumps(legacy)
        digest = "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()
        db.execute("UPDATE experiments SET record_json=?, content_hash=?", (encoded, digest))
    restored = SQLiteExperimentTracker(path).get(spec.experiment_id)
    assert restored.report.receipt is None and restored.report.schema_version == "1.0"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT record_json FROM experiments").fetchone()[0] == encoded


@pytest.mark.parametrize("change", ["artifact", "runtime", "version", "verification", "checks"])
def test_report_rejects_inconsistent_receipt(tmp_path, change):
    task, store, snapshot = _snapshot(tmp_path)
    report = _run(
        DataScienceWorkbench(artifact_store=store), snapshot, _spec(task, snapshot.snapshot_id)
    )
    payload = report.model_dump(mode="json")
    receipt = payload["receipt"]
    if change == "artifact":
        receipt["result_refs"][0]["artifact_id"] = str(uuid4())
    elif change == "runtime":
        receipt["method"]["runtime_fingerprint"] = "sha256:" + "a" * 64
    elif change == "version":
        receipt["method"]["method_version"] = "9.9"
    elif change == "verification":
        receipt["verification"]["scope"] = "authoritative_rederivation"
    else:
        receipt["verification"]["check_codes"] = ["made_up"]
    with pytest.raises(ValidationError, match="receipt"):
        ExperimentReport.model_validate(payload)


@pytest.mark.parametrize("seed", [0, 2**32 - 1])
def test_method_seed_bounds_and_legacy_default(seed):
    method = ExecutionMethod(method_id="science.baseline", method_version="1.0", random_seed=seed)
    assert ExecutionMethod.model_validate_json(method.model_dump_json()).random_seed == seed
    assert ExecutionMethod(method_id="rules.workflow", method_version="v1").random_seed is None


@pytest.mark.parametrize("seed", [-1, 2**32])
def test_invalid_method_seed_is_rejected(seed):
    with pytest.raises(ValidationError):
        ExecutionMethod(method_id="science.baseline", method_version="1.0", random_seed=seed)


@pytest.mark.parametrize("change", ["random_seed", "task_id", "estimator", "standardize"])
def test_tracker_rejects_receipt_different_from_spec(tmp_path, change):
    task, store, snapshot = _snapshot(tmp_path)
    spec = _spec(task, snapshot.snapshot_id)
    report = _run(DataScienceWorkbench(artifact_store=store), snapshot, spec)
    tracker = SQLiteExperimentTracker(tmp_path / "tracker.sqlite")
    if change == "estimator":
        altered = spec
        data = report.model_dump(mode="json")
        data["receipt"]["method"]["method_id"] = "science.linear_regression"
        report = ExperimentReport.model_validate(data)
    else:
        value = {"task_id": uuid4(), "random_seed": 99, "standardize": False}[change]
        altered = ExperimentSpec.model_validate({**spec.model_dump(), change: value})
    with pytest.raises(ValidationError, match="receipt must match its spec"):
        tracker.record(spec=altered, report=report)


@pytest.mark.parametrize("fault", ["gate", "sandbox", "completion_audit"])
def test_execution_failures_do_not_return_receipt(tmp_path, monkeypatch, fault):
    from generaldataagent.data_science.sandbox import FixedTemplateSandbox, SandboxError
    from generaldataagent.data_science.workbench import ScienceGateError
    from generaldataagent.observability import JsonlAuditSink

    task, store, snapshot = _snapshot(tmp_path)
    spec = _spec(task, snapshot.snapshot_id)
    audit = JsonlAuditSink(tmp_path / "audit.jsonl")
    workbench = DataScienceWorkbench(artifact_store=store, audit=audit)
    if fault == "gate":
        import generaldataagent.data_science.workbench as module

        prepare = module.prepare_experiment

        def blocked(rows, spec):
            prepared = prepare(rows, spec)
            from dataclasses import replace

            from generaldataagent.contracts import ScienceCheck

            return replace(
                prepared,
                checks=(
                    ScienceCheck(
                        check_id="blocked",
                        passed=False,
                        severity="error",
                        code="blocked",
                        message="Synthetic failing data gate.",
                    ),
                ),
            )

        monkeypatch.setattr(module, "prepare_experiment", blocked)
        error = ScienceGateError
    elif fault == "sandbox":

        def broken(self, **kwargs):
            raise SandboxError("synthetic worker failure")

        monkeypatch.setattr(FixedTemplateSandbox, "run", broken)
        error = SandboxError
    else:
        emit = audit.emit

        def fail_completion(event):
            if event.event_type == "science.experiment_completed":
                raise OSError("synthetic completion audit outage")
            emit(event)

        monkeypatch.setattr(audit, "emit", fail_completion)
        error = OSError
    with pytest.raises(error):
        _run(workbench, snapshot, spec)
    artifacts = list(store.root.rglob("*.json"))
    assert len(artifacts) == (2 if fault == "completion_audit" else 1)


def test_composed_identity_and_trace_reach_science_receipt(tmp_path):
    from test_execution_composition import ACTOR, _science_fixture

    from generaldataagent.observability import JsonlAuditSink
    from generaldataagent.orchestration.composition import ExecutionComposition

    store, snapshot, spec = _science_fixture(tmp_path)
    composition = ExecutionComposition(
        mode="research",
        actor=ACTOR,
        artifacts=store,
        audit=JsonlAuditSink(tmp_path / "audit.jsonl"),
        task_id=spec.task_id,
        trace_id="composed-science",
    )
    report = DataScienceWorkbench(artifact_store=store, composition=composition).run_experiment(
        snapshot=snapshot,
        spec=spec,
        user_id=ACTOR.user_id,
        tenant_id=ACTOR.tenant_id,
        project_id=ACTOR.project_id,
        data_domain="payments",
        trace_id="caller-science",
    )
    assert report.receipt.task_id == str(spec.task_id)
    assert report.receipt.trace_id == "composed-science"
