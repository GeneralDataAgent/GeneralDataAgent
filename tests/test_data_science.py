import json
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.contracts import (
    ActorContext,
    AgentRole,
    ExperimentSpec,
    ModelRegistrationDraft,
    OCIContainerRuntime,
    RiskLevel,
    SandboxQuota,
    SnapshotColumn,
    SplitSpec,
    ToolRequest,
)
from generaldataagent.data_science.registration import ModelRegistrationWorkflow
from generaldataagent.data_science.sandbox import (
    FixedTemplateSandbox,
    OCIContainerSandbox,
    SandboxError,
)
from generaldataagent.data_science.snapshot import create_dataset_snapshot
from generaldataagent.data_science.tracking import (
    ExperimentTrackingError,
    MirroringExperimentTracker,
    SQLiteExperimentTracker,
)
from generaldataagent.data_science.workbench import DataScienceWorkbench, ScienceGateError
from generaldataagent.observability import JsonlAuditSink
from generaldataagent.orchestration.composition import ExecutionComposition
from generaldataagent.policy import ApprovalError, InMemoryApprovalStore, PolicyEngine
from generaldataagent.security.science_scope import ScienceWorkloadScope


def _classification_rows() -> list[dict[str, object]]:
    return [
        {
            "age": 20 + index,
            "score": (index * 7) % 19,
            "customer_group": f"g{index % 4}",
            "observed_at": f"2026-01-{index + 1:02d}",
            "converted": "yes" if index % 3 == 0 else "no",
        }
        for index in range(20)
    ]


def _columns() -> tuple[SnapshotColumn, ...]:
    return (
        SnapshotColumn(name="age", data_type="integer", role="feature"),
        SnapshotColumn(name="score", data_type="integer", role="feature"),
        SnapshotColumn(name="customer_group", data_type="string", role="group"),
        SnapshotColumn(name="observed_at", data_type="string", role="timestamp"),
        SnapshotColumn(name="converted", data_type="string", role="target"),
    )


def _snapshot(tmp_path: Path):
    task_id = uuid4()
    store = LocalArtifactStore(tmp_path / "artifacts")
    snapshot = create_dataset_snapshot(
        task_id=task_id,
        tenant_id="tenant-a",
        project_id="project-a",
        data_domain="commerce",
        source_name="unit-fixture",
        rows=_classification_rows(),
        columns=_columns(),
        artifact_store=store,
    )
    return task_id, store, snapshot


def _spec(task_id, snapshot_id, *, split=None, quota=None) -> ExperimentSpec:
    return ExperimentSpec(
        task_id=task_id,
        snapshot_id=snapshot_id,
        problem_type="binary_classification",
        target_column="converted",
        feature_columns=("age", "score"),
        baseline="majority",
        primary_metric="f1",
        split=split or SplitSpec(),
        quota=quota or SandboxQuota(),
    )


def _run(workbench, snapshot, spec):
    return workbench.run_experiment(
        snapshot=snapshot,
        spec=spec,
        user_id="scientist",
        tenant_id="tenant-a",
        project_id="project-a",
        data_domain="commerce",
    )


def test_snapshot_is_immutable_and_content_addressed(tmp_path: Path) -> None:
    _, store, snapshot = _snapshot(tmp_path)
    assert snapshot.row_count == 20
    assert snapshot.content_hash == snapshot.data_artifact.content_hash
    path = store.root / snapshot.data_artifact.uri.removeprefix("artifact://local/")
    path.write_text("[]")
    with pytest.raises(ValueError, match="content hash mismatch"):
        store.read_bytes(snapshot.data_artifact)


def test_snapshot_rejects_values_that_conflict_with_declared_type(tmp_path: Path) -> None:
    rows = _classification_rows()
    rows[0]["age"] = "twenty"
    with pytest.raises(ValueError, match="declared type integer"):
        create_dataset_snapshot(
            task_id=uuid4(),
            tenant_id="tenant-a",
            project_id="project-a",
            data_domain="commerce",
            source_name="invalid-types",
            rows=rows,
            columns=_columns(),
            artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        )


def test_experiment_contract_rejects_metric_misuse() -> None:
    with pytest.raises(ValidationError, match="classification metric"):
        ExperimentSpec(
            task_id=uuid4(),
            snapshot_id=uuid4(),
            problem_type="binary_classification",
            target_column="target",
            feature_columns=("feature",),
            baseline="majority",
            primary_metric="rmse",
        )


def test_fixed_template_experiment_is_reproducible(tmp_path: Path) -> None:
    task_id, store, snapshot = _snapshot(tmp_path)
    workbench = DataScienceWorkbench(artifact_store=store)
    spec = _spec(task_id, snapshot.snapshot_id)
    first = _run(workbench, snapshot, spec)
    second = _run(workbench, snapshot, spec)
    assert first.passed and second.passed
    assert first.metrics == second.metrics
    assert first.eda == second.eda
    assert "feature_standardized_mean_difference" in first.statistical_checks
    assert "target_shift" in first.statistical_checks
    target_shift = first.statistical_checks["target_shift"]
    assert 0 <= target_shift["two_sided_normal_p_value"] <= 1
    assert first.runtime_fingerprint == second.runtime_fingerprint
    assert first.output_artifact.lineage == (snapshot.data_artifact.artifact_id,)


def test_experiment_writes_correlated_audit_events(tmp_path: Path) -> None:
    task_id, store, snapshot = _snapshot(tmp_path)
    audit_path = tmp_path / "audit" / "events.jsonl"
    workbench = DataScienceWorkbench(
        artifact_store=store,
        audit=JsonlAuditSink(audit_path),
    )
    _run(workbench, snapshot, _spec(task_id, snapshot.snapshot_id))
    events = [json.loads(line) for line in audit_path.read_text().splitlines() if line]
    assert len(events) == 2
    assert events[0]["trace_id"] == events[1]["trace_id"]
    assert events[0]["event_type"] == "science.policy_decision"
    assert events[1]["event_type"] == "science.experiment_completed"


def test_experiment_tracker_survives_restart_and_rejects_overwrite(tmp_path: Path) -> None:
    task_id, store, snapshot = _snapshot(tmp_path)
    tracker_path = tmp_path / "tracking" / "experiments.sqlite"
    tracker = SQLiteExperimentTracker(tracker_path)
    workbench = DataScienceWorkbench(artifact_store=store, tracker=tracker)
    spec = _spec(task_id, snapshot.snapshot_id)
    report = _run(workbench, snapshot, spec)
    restored = SQLiteExperimentTracker(tracker_path).get(spec.experiment_id)
    assert restored.report == report
    assert SQLiteExperimentTracker(tracker_path).list_for_snapshot(snapshot.snapshot_id) == (
        restored,
    )
    with pytest.raises(ExperimentTrackingError, match="already recorded"):
        _run(workbench, snapshot, spec)


def test_experiment_tracker_detects_record_tampering(tmp_path: Path) -> None:
    import sqlite3

    task_id, store, snapshot = _snapshot(tmp_path)
    tracker = SQLiteExperimentTracker(tmp_path / "tracking.sqlite")
    spec = _spec(task_id, snapshot.snapshot_id)
    _run(DataScienceWorkbench(artifact_store=store, tracker=tracker), snapshot, spec)
    with sqlite3.connect(tracker.path) as connection:
        connection.execute(
            "UPDATE experiments SET record_json = ? WHERE experiment_id = ?",
            ("{}", str(spec.experiment_id)),
        )
    with pytest.raises(ExperimentTrackingError, match="integrity"):
        tracker.get(spec.experiment_id)


@pytest.mark.parametrize(
    "split",
    [
        SplitSpec(strategy="temporal", timestamp_column="observed_at"),
        SplitSpec(strategy="group", group_column="customer_group"),
    ],
)
def test_controlled_split_strategies_pass(tmp_path: Path, split: SplitSpec) -> None:
    task_id, store, snapshot = _snapshot(tmp_path)
    report = _run(
        DataScienceWorkbench(artifact_store=store),
        snapshot,
        _spec(task_id, snapshot.snapshot_id, split=split),
    )
    assert report.train_rows + report.test_rows == snapshot.row_count
    assert all(check.passed for check in report.checks)


def test_exact_target_proxy_fails_closed(tmp_path: Path) -> None:
    rows = _classification_rows()
    for row in rows:
        row["leaked"] = row["converted"]
    columns = (*_columns(), SnapshotColumn(name="leaked", data_type="string", role="feature"))
    rows = [{name: row[name] for name in [column.name for column in columns]} for row in rows]
    task_id = uuid4()
    store = LocalArtifactStore(tmp_path / "artifacts")
    snapshot = create_dataset_snapshot(
        task_id=task_id,
        tenant_id="tenant-a",
        project_id="project-a",
        data_domain="commerce",
        source_name="leaky-fixture",
        rows=rows,
        columns=columns,
        artifact_store=store,
    )
    spec = _spec(task_id, snapshot.snapshot_id).model_copy(
        update={"feature_columns": ("age", "leaked")}
    )
    with pytest.raises(ScienceGateError, match="exact_target_proxy"):
        _run(DataScienceWorkbench(artifact_store=store), snapshot, spec)


def test_duplicate_rows_crossing_random_split_fail_closed(tmp_path: Path) -> None:
    rows = _classification_rows()
    rows[3] = dict(rows[0])
    task_id = uuid4()
    store = LocalArtifactStore(tmp_path / "artifacts")
    snapshot = create_dataset_snapshot(
        task_id=task_id,
        tenant_id="tenant-a",
        project_id="project-a",
        data_domain="commerce",
        source_name="duplicate-fixture",
        rows=rows,
        columns=_columns(),
        artifact_store=store,
    )
    spec = _spec(task_id, snapshot.snapshot_id).model_copy(update={"random_seed": 3})
    with pytest.raises(ScienceGateError) as caught:
        _run(DataScienceWorkbench(artifact_store=store), snapshot, spec)
    assert any(check.code == "cross_split_duplicates" for check in caught.value.checks)


def test_sandbox_enforces_input_quota(tmp_path: Path) -> None:
    task_id, _, snapshot = _snapshot(tmp_path)
    spec = _spec(
        task_id,
        snapshot.snapshot_id,
        quota=SandboxQuota(max_input_bytes=1024),
    )
    with pytest.raises(SandboxError, match="input exceeds"):
        FixedTemplateSandbox().run(
            rows=_classification_rows(),
            spec=spec,
            train_indices=list(range(15)),
            test_indices=list(range(15, 20)),
        )


def test_oci_sandbox_uses_digest_and_mandatory_isolation(monkeypatch) -> None:
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        output = {
            "template_version": "1.0",
            "train_rows": 15,
            "test_rows": 5,
            "metrics": {"accuracy": 0.5, "precision": 0.0, "recall": 0.0, "f1": 0.0},
            "eda": {},
            "statistical_checks": {},
            "error_analysis": {},
            "subgroups": [],
            "model": {"algorithm": "majority"},
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(output).encode(), b"")

    monkeypatch.setattr("generaldataagent.data_science.sandbox.subprocess.run", fake_run)
    digest = f"sha256:{'b' * 64}"
    sandbox = OCIContainerSandbox(
        OCIContainerRuntime(
            engine="docker",
            image_ref=f"registry.example/gda-stage3:1.0@{digest}",
        )
    )
    spec = _spec(uuid4(), uuid4())
    result = sandbox.run(
        rows=_classification_rows(),
        spec=spec,
        train_indices=list(range(15)),
        test_indices=list(range(15, 20)),
    )
    command = captured["command"]
    assert result["template_version"] == "1.0"
    assert sandbox.runtime_fingerprint == digest
    assert "--pull=never" in command
    assert "--network=none" in command
    assert "--read-only" in command
    assert "--cap-drop=ALL" in command
    assert "--security-opt=no-new-privileges:true" in command
    assert "--user=65532:65532" in command
    assert not any(item.startswith("--volume") or item.startswith("--env") for item in command)
    assert captured["kwargs"]["check"] is False


def test_oci_sandbox_rejects_result_with_wrong_split_counts(monkeypatch) -> None:
    output = {
        "template_version": "1.0",
        "train_rows": 999,
        "test_rows": 5,
        "metrics": {"accuracy": 0.5, "precision": 0.0, "recall": 0.0, "f1": 0.0},
        "eda": {},
        "statistical_checks": {},
        "error_analysis": {},
        "subgroups": [],
        "model": {},
    }

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, json.dumps(output).encode(), b"")

    monkeypatch.setattr("generaldataagent.data_science.sandbox.subprocess.run", fake_run)
    sandbox = OCIContainerSandbox(
        OCIContainerRuntime(
            engine="docker",
            image_ref=f"registry.example/gda-stage3@sha256:{'d' * 64}",
        )
    )
    with pytest.raises(SandboxError, match="split counts"):
        sandbox.run(
            rows=_classification_rows(),
            spec=_spec(uuid4(), uuid4()),
            train_indices=list(range(15)),
            test_indices=list(range(15, 20)),
        )


def test_oci_runtime_rejects_mutable_image_tag() -> None:
    with pytest.raises(ValidationError, match="image_ref"):
        OCIContainerRuntime(engine="docker", image_ref="generaldataagent-stage3:latest")


def test_oci_sandbox_fails_closed_when_engine_is_missing(monkeypatch) -> None:
    def missing_engine(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr("generaldataagent.data_science.sandbox.subprocess.run", missing_engine)
    sandbox = OCIContainerSandbox(
        OCIContainerRuntime(
            engine="podman",
            image_ref=f"registry.example/gda-stage3@sha256:{'c' * 64}",
        )
    )
    with pytest.raises(SandboxError, match="engine is unavailable"):
        sandbox.run(
            rows=_classification_rows(),
            spec=_spec(uuid4(), uuid4()),
            train_indices=list(range(15)),
            test_indices=list(range(15, 20)),
        )


def test_stage3_container_build_is_pinned_and_non_root() -> None:
    dockerfile = (
        Path(__file__).parents[1] / "infra" / "sandbox" / "stage3" / "Dockerfile"
    ).read_text()
    assert "python:3.12.13-slim@sha256:" in dockerfile
    assert "USER 65532:65532" in dockerfile
    assert 'ENTRYPOINT ["python", "-I", "-S", "/app/worker.py"]' in dockerfile
    assert "apt-get" not in dockerfile
    assert "pip install" not in dockerfile


def test_policy_only_allows_fixed_science_template() -> None:
    base = dict(
        task_id=uuid4(),
        user_id="scientist",
        tenant_id="tenant-a",
        project_id="project-a",
        agent_role=AgentRole.DATA_SCIENTIST,
        resource="snapshot",
        arguments={},
        data_domains=("commerce",),
        requested_risk=RiskLevel.R1,
    )
    engine = PolicyEngine()
    allowed = engine.evaluate(
        ToolRequest(
            **base,
            tool_name="python.experiment",
            action="run_fixed_template",
        )
    )
    denied = engine.evaluate(ToolRequest(**base, tool_name="python.experiment", action="exec"))
    deployment = engine.evaluate(
        ToolRequest(
            **(base | {"requested_risk": RiskLevel.R3}),
            tool_name="model.registry",
            action="deploy",
        )
    )
    assert allowed.decision == "allow"
    assert denied.decision == "deny"
    assert deployment.decision == "deny"


def test_subgroup_evaluation_is_structured_and_warns_on_small_groups(tmp_path: Path) -> None:
    task_id, store, snapshot = _snapshot(tmp_path)
    spec = _spec(task_id, snapshot.snapshot_id).model_copy(
        update={"subgroup_columns": ("customer_group",), "minimum_subgroup_rows": 3}
    )
    report = _run(DataScienceWorkbench(artifact_store=store), snapshot, spec)
    assert report.subgroups
    assert {group.column for group in report.subgroups} == {"customer_group"}
    assert all(group.metrics[0].name == "accuracy" for group in report.subgroups)
    assert any(check.code == "subgroup_too_small" for check in report.checks)
    assert report.passed is True


def test_model_card_and_registration_require_independent_one_time_approval(tmp_path: Path) -> None:
    task_id, store, snapshot = _snapshot(tmp_path)
    workbench = DataScienceWorkbench(artifact_store=store)
    report = _run(workbench, snapshot, _spec(task_id, snapshot.snapshot_id))
    card = workbench.build_model_card(
        snapshot=snapshot,
        report=report,
        model_name="conversion-baseline",
        model_version="0.1.0",
        intended_use="Compare future candidate models on the governed offline dataset.",
        owner="data-science",
        out_of_scope_uses=("Automated customer decisions",),
        ethical_considerations=("Evaluate subgroup performance before any pilot.",),
        limitations=("Majority baseline has no individual predictive power.",),
        monitoring_requirements=("Re-evaluate after snapshot drift.",),
        subgroup_evaluation="Held-out subgroup rows are too small for a deployment decision.",
    )
    approvals = InMemoryApprovalStore()
    workflow = ModelRegistrationWorkflow(approvals=approvals)
    draft, request = workflow.request_review(
        task_id=task_id,
        model_card=card,
        model_artifact=report.output_artifact,
        user_id="scientist",
        tenant_id="tenant-a",
        project_id="project-a",
        data_domain="commerce",
    )
    assert draft.status == "pending_review"
    assert draft.deployment_permitted is False
    with pytest.raises(ApprovalError, match="cannot approve"):
        approvals.approve(request.approval_id, approver_id="scientist")
    approvals.approve(request.approval_id, approver_id="model-risk-reviewer")
    approved = workflow.finalize_approved_draft(
        draft=draft,
        approval_id=request.approval_id,
        user_id="scientist",
        tenant_id="tenant-a",
        project_id="project-a",
        data_domain="commerce",
    )
    assert approved.status == "approved_draft"
    assert approved.deployment_permitted is False
    with pytest.raises(ApprovalError, match="already been consumed"):
        workflow.finalize_approved_draft(
            draft=draft,
            approval_id=request.approval_id,
            user_id="scientist",
            tenant_id="tenant-a",
            project_id="project-a",
            data_domain="commerce",
        )


def test_registration_contract_never_permits_deployment(tmp_path: Path) -> None:
    task_id, store, snapshot = _snapshot(tmp_path)
    workbench = DataScienceWorkbench(artifact_store=store)
    report = _run(workbench, snapshot, _spec(task_id, snapshot.snapshot_id))
    card = workbench.build_model_card(
        snapshot=snapshot,
        report=report,
        model_name="baseline",
        model_version="1",
        intended_use="Offline evaluation only.",
        owner="owner",
        out_of_scope_uses=("Deployment",),
        ethical_considerations=("Human review required.",),
        limitations=("Baseline only.",),
        monitoring_requirements=("Check drift.",),
        subgroup_evaluation="No production subgroup claim; offline baseline only.",
    )
    with pytest.raises(ValidationError):
        ModelRegistrationDraft(
            task_id=task_id,
            model_card=card,
            model_artifact=report.output_artifact,
            deployment_permitted=True,
        )


def test_workbench_runs_the_injected_sandbox_through_the_shared_preparation(
    tmp_path: Path,
) -> None:
    class Recorder:
        runtime_fingerprint = "sha256:" + "a" * 64

        def __init__(self):
            self.calls = []

        def run(self, *, rows, spec, train_indices, test_indices, notebook=None):
            self.calls.append((rows, spec, train_indices, test_indices))
            return {
                "template_version": "1.0",
                "train_rows": len(train_indices),
                "test_rows": len(test_indices),
                "metrics": {"accuracy": 0.5},
                "eda": {},
                "statistical_checks": {},
                "error_analysis": {},
                "subgroups": [],
            }

    task_id, store, snapshot = _snapshot(tmp_path)
    sandbox = Recorder()
    workbench = DataScienceWorkbench(artifact_store=store, sandbox=sandbox)
    report = _run(workbench, snapshot, _spec(task_id, snapshot.snapshot_id))
    assert report.passed is True
    assert report.runtime_fingerprint == "sha256:" + "a" * 64
    assert report.receipt.method.runtime_fingerprint == sandbox.runtime_fingerprint
    assert report.receipt.method.random_seed == 42
    assert len(sandbox.calls) == 1
    rows, spec, train, test = sandbox.calls[0]
    assert len(rows) == 20 and sorted(train + test) == list(range(20))
    assert spec.task_id == task_id


def _rewrite_stored_record(tracker, experiment_id, mutate):
    """Rewrite one stored record the way another producer could have written it."""
    import hashlib
    import sqlite3

    with sqlite3.connect(tracker.path) as connection:
        (content,) = connection.execute(
            "SELECT record_json FROM experiments WHERE experiment_id = ?",
            (str(experiment_id),),
        ).fetchone()
    record = json.loads(content)
    mutate(record)
    rewritten = json.dumps(record, sort_keys=True)
    with sqlite3.connect(tracker.path) as connection:
        connection.execute(
            "UPDATE experiments SET record_json = ?, content_hash = ? WHERE experiment_id = ?",
            (
                rewritten,
                "sha256:" + hashlib.sha256(rewritten.encode()).hexdigest(),
                str(experiment_id),
            ),
        )


def _rewrite_stored_version(tracker, experiment_id, version):
    def mutate(record):
        if version is None:
            del record["report"]["receipt"]["receipt_version"]
        else:
            record["report"]["receipt"]["receipt_version"] = version

    _rewrite_stored_record(tracker, experiment_id, mutate)


def test_a_stored_experiment_from_another_receipt_version_is_refused_by_name(tmp_path: Path):
    from generaldataagent.contracts import ReceiptVersionUnsupported

    task_id, store, snapshot = _snapshot(tmp_path)
    tracker = SQLiteExperimentTracker(tmp_path / "tracking.sqlite")
    spec = _spec(task_id, snapshot.snapshot_id)
    _run(DataScienceWorkbench(artifact_store=store, tracker=tracker), snapshot, spec)
    _rewrite_stored_version(tracker, spec.experiment_id, "1.1")
    with pytest.raises(ReceiptVersionUnsupported):
        tracker.get(spec.experiment_id)


def test_a_stored_experiment_without_a_receipt_version_is_refused(tmp_path: Path):
    task_id, store, snapshot = _snapshot(tmp_path)
    tracker = SQLiteExperimentTracker(tmp_path / "tracking.sqlite")
    spec = _spec(task_id, snapshot.snapshot_id)
    _run(DataScienceWorkbench(artifact_store=store, tracker=tracker), snapshot, spec)
    _rewrite_stored_version(tracker, spec.experiment_id, None)
    with pytest.raises(ValueError, match="must state its receipt_version"):
        tracker.get(spec.experiment_id)


def test_a_stored_experiment_with_a_stronger_verification_is_refused_by_name(tmp_path: Path):
    from generaldataagent.data_science.tracking import ExperimentTrackingError

    task_id, store, snapshot = _snapshot(tmp_path)
    tracker = SQLiteExperimentTracker(tmp_path / "tracking.sqlite")
    spec = _spec(task_id, snapshot.snapshot_id)
    _run(DataScienceWorkbench(artifact_store=store, tracker=tracker), snapshot, spec)
    _rewrite_stored_record(
        tracker,
        spec.experiment_id,
        lambda record: record["report"]["receipt"]["verification"].update(
            scope="independent_implementation", verifier="outside-auditor"
        ),
    )
    # This tracker writes producer self-checks only, so a stronger claim is
    # refused as its own named failure rather than as an internal schema error.
    with pytest.raises(
        ExperimentTrackingError, match="experiment_receipt_verification_scope_mismatch"
    ):
        tracker.get(spec.experiment_id)


def test_a_record_without_a_receipt_stays_readable_and_unrewritten(tmp_path: Path):
    import sqlite3

    task_id, store, snapshot = _snapshot(tmp_path)
    tracker = SQLiteExperimentTracker(tmp_path / "tracking.sqlite")
    spec = _spec(task_id, snapshot.snapshot_id)
    _run(DataScienceWorkbench(artifact_store=store, tracker=tracker), snapshot, spec)
    _rewrite_stored_record(
        tracker, spec.experiment_id, lambda record: record["report"].pop("receipt")
    )

    def stored():
        with sqlite3.connect(tracker.path) as connection:
            return connection.execute(
                "SELECT record_json, content_hash FROM experiments WHERE experiment_id = ?",
                (str(spec.experiment_id),),
            ).fetchone()

    before = stored()
    tracked = tracker.get(spec.experiment_id)
    assert tracked.report.receipt is None
    assert stored() == before


class _Mirror:
    backend_name = "fake-index"

    def __init__(self, *, admitted=True, fail=False):
        self._admitted = admitted
        self.fail = fail
        self.published = []

    def admitted(self):
        return self._admitted

    def publish(self, tracked):
        if self.fail:
            raise RuntimeError("service down")
        self.published.append(tracked.experiment_id)
        return f"ext-{tracked.experiment_id}"


def test_a_mirror_failure_never_changes_the_local_record(tmp_path: Path) -> None:
    task_id, store, snapshot = _snapshot(tmp_path)
    local = SQLiteExperimentTracker(tmp_path / "tracking.sqlite")
    tracker = MirroringExperimentTracker(local, _Mirror(fail=True))
    workbench = DataScienceWorkbench(artifact_store=store, tracker=tracker)
    spec = _spec(task_id, snapshot.snapshot_id)
    report = _run(workbench, snapshot, spec)
    # The experiment happened; the mirror lost its copy, not the evidence.
    assert local.get(spec.experiment_id).report == report
    status = tracker.mirror_status(spec.experiment_id)
    assert status["status"] == "pending"
    assert status["attempts"] == 1
    assert status["last_error"] == "RuntimeError: service down"


def test_a_pending_mirror_write_is_retried_and_then_delivered(tmp_path: Path) -> None:
    task_id, store, snapshot = _snapshot(tmp_path)
    local = SQLiteExperimentTracker(tmp_path / "tracking.sqlite")
    mirror = _Mirror(fail=True)
    tracker = MirroringExperimentTracker(local, mirror)
    spec = _spec(task_id, snapshot.snapshot_id)
    _run(DataScienceWorkbench(artifact_store=store, tracker=tracker), snapshot, spec)
    mirror.fail = False
    assert tracker.publish_pending() == 1
    assert tracker.mirror_status(spec.experiment_id)["status"] == "delivered"
    assert mirror.published == [spec.experiment_id]
    assert tracker.publish_pending() == 0


def test_a_delivered_mirror_still_leaves_the_local_record_authoritative(tmp_path: Path) -> None:
    task_id, store, snapshot = _snapshot(tmp_path)
    local = SQLiteExperimentTracker(tmp_path / "tracking.sqlite")
    tracker = MirroringExperimentTracker(local, _Mirror())
    spec = _spec(task_id, snapshot.snapshot_id)
    report = _run(DataScienceWorkbench(artifact_store=store, tracker=tracker), snapshot, spec)
    assert tracker.mirror_status(spec.experiment_id)["status"] == "delivered"
    assert tracker.get(spec.experiment_id).report == report
    assert tracker.list_for_snapshot(snapshot.snapshot_id)[0].experiment_id == spec.experiment_id


def test_a_mirror_must_be_admitted_before_it_is_wired(tmp_path: Path) -> None:
    local = SQLiteExperimentTracker(tmp_path / "tracking.sqlite")
    with pytest.raises(ValueError, match="not admitted"):
        MirroringExperimentTracker(local, _Mirror(admitted=False))


def test_an_unmirrored_experiment_reports_no_attempt(tmp_path: Path) -> None:
    local = SQLiteExperimentTracker(tmp_path / "tracking.sqlite")
    tracker = MirroringExperimentTracker(local, _Mirror())
    assert tracker.mirror_status(uuid4()) == {
        "status": "not_attempted",
        "attempts": 0,
        "last_error": None,
    }


def _scope(task_id, snapshot_id, **overrides):
    return ScienceWorkloadScope(
        user_id=overrides.get("user_id", "scientist"),
        tenant_id=overrides.get("tenant_id", "tenant-a"),
        project_id=overrides.get("project_id", "project-a"),
        data_domains=frozenset(overrides.get("data_domains", {"commerce"})),
        task_id=overrides.get("task_id", task_id),
        snapshot_id=overrides.get("snapshot_id", snapshot_id),
    )


def test_a_workload_scope_lets_the_bound_snapshot_run(tmp_path: Path) -> None:
    task_id, store, snapshot = _snapshot(tmp_path)
    scope = _scope(task_id, snapshot.snapshot_id)
    workbench = DataScienceWorkbench(artifact_store=store, workload_scope=scope)
    report = _run(workbench, snapshot, _spec(task_id, snapshot.snapshot_id))
    assert report.passed is True


def test_a_workload_scope_refuses_another_snapshot(tmp_path: Path) -> None:
    task_id, store, snapshot = _snapshot(tmp_path)
    scope = _scope(task_id, uuid4())
    workbench = DataScienceWorkbench(artifact_store=store, workload_scope=scope)
    with pytest.raises(PermissionError, match="science_workload_snapshot_mismatch"):
        _run(workbench, snapshot, _spec(task_id, snapshot.snapshot_id))


def test_a_workload_scope_refuses_another_task(tmp_path: Path) -> None:
    task_id, store, snapshot = _snapshot(tmp_path)
    workbench = DataScienceWorkbench(
        artifact_store=store, workload_scope=_scope(uuid4(), snapshot.snapshot_id)
    )
    with pytest.raises(PermissionError, match="science_workload_task_mismatch"):
        _run(workbench, snapshot, _spec(task_id, snapshot.snapshot_id))


def test_a_workload_scope_refuses_another_actor(tmp_path: Path) -> None:
    task_id, store, snapshot = _snapshot(tmp_path)
    scope = _scope(task_id, snapshot.snapshot_id, user_id="someone-else")
    workbench = DataScienceWorkbench(artifact_store=store, workload_scope=scope)
    with pytest.raises(PermissionError, match="science_workload_actor_mismatch"):
        _run(workbench, snapshot, _spec(task_id, snapshot.snapshot_id))


def test_a_scope_that_contradicts_the_composition_is_refused(tmp_path: Path) -> None:
    task_id, store, snapshot = _snapshot(tmp_path)
    composition = ExecutionComposition(
        mode="research",
        actor=ActorContext(
            user_id="scientist",
            tenant_id="tenant-a",
            project_id="project-a",
            data_domains=("commerce",),
        ),
        artifacts=store,
        audit=JsonlAuditSink(tmp_path / "audit.jsonl"),
        workload_scope=_scope(task_id, snapshot.snapshot_id),
    )
    with pytest.raises(ValueError, match="disagrees_with_composition"):
        DataScienceWorkbench(
            artifact_store=store,
            composition=composition,
            workload_scope=_scope(task_id, uuid4()),
        )
