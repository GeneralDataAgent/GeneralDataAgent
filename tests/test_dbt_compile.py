import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.connectors import SQLiteConnector
from generaldataagent.contracts import ActorContext
from generaldataagent.data_engineering import DataEngineeringRuntime, DbtCompileRunner
from generaldataagent.observability import JsonlAuditSink
from generaldataagent.policy import ApprovalError, InMemoryApprovalStore
from generaldataagent.tools import PolicyDeniedError


def create_runner(tmp_path) -> DbtCompileRunner:
    project = tmp_path / "analytics"
    profiles = tmp_path / "profiles"
    project.mkdir()
    profiles.mkdir()
    (project / "dbt_project.yml").write_text("name: analytics\nversion: 1.0.0\n")
    script = tmp_path / "fake_dbt.py"
    script.write_text(
        "import sys\n"
        "print('fake dbt ' + ' '.join(sys.argv[1:]))\n"
        "raise SystemExit(0 if sys.argv[1] in {'compile', 'test'} else 2)\n"
    )
    return DbtCompileRunner(
        project_dir=project,
        profiles_dir=profiles,
        executable=(sys.executable, str(script)),
        allowed_targets=frozenset({"ci"}),
    )


def test_dbt_compile_uses_pinned_project_and_safe_target(tmp_path) -> None:
    runner = create_runner(tmp_path)

    result = runner.compile(target="ci", selected_resources=("model.orders+",))

    assert result.succeeded is True
    assert result.exit_code == 0
    assert result.target == "ci"
    assert result.selected_resources == ("model.orders+",)
    assert result.production_write_enabled is False
    assert result.command[:2] == ("dbt", "compile")
    assert str(tmp_path) not in " ".join(result.command)
    assert str(tmp_path) not in result.stdout_tail
    assert str(tmp_path) not in result.stderr_tail
    assert "fake dbt compile" in result.stdout_tail


def test_dbt_compile_rejects_production_target_before_execution(tmp_path) -> None:
    runner = create_runner(tmp_path)

    with pytest.raises(PermissionError, match="not approved"):
        runner.compile(target="prod")


def test_dbt_compile_rejects_unsafe_selector(tmp_path) -> None:
    runner = create_runner(tmp_path)

    with pytest.raises(ValueError, match="invalid dbt selectors"):
        runner.compile(target="ci", selected_resources=("model.orders;rm",))


def test_runtime_policy_gates_and_persists_dbt_compile(tmp_path) -> None:
    database = tmp_path / "source.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE orders (id INTEGER)")
    runtime = DataEngineeringRuntime(
        connectors={"warehouse": SQLiteConnector(database, connector_id="warehouse")},
        connector_domains={"warehouse": frozenset({"commerce"})},
        artifacts=LocalArtifactStore(tmp_path / "artifacts"),
        dbt_runner=create_runner(tmp_path),
    )
    actor = ActorContext(
        user_id="user-1",
        tenant_id="tenant-1",
        project_id="project-1",
        data_domains=("commerce",),
    )

    result = runtime.compile_dbt(
        task_id=uuid4(),
        actor=actor,
        connector_id="warehouse",
        target="ci",
        selected_resources=("model.orders",),
        trace_id="dbt-compile",
    )

    assert result.succeeded is True
    assert list((tmp_path / "artifacts").rglob("*.json"))


def test_dbt_test_requires_exact_one_time_r2_approval(tmp_path) -> None:
    database = tmp_path / "source.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE orders (id INTEGER)")
    approvals = InMemoryApprovalStore()
    audit_path = tmp_path / "audit.jsonl"
    runtime = DataEngineeringRuntime(
        connectors={"warehouse": SQLiteConnector(database, connector_id="warehouse")},
        connector_domains={"warehouse": frozenset({"commerce"})},
        artifacts=LocalArtifactStore(tmp_path / "artifacts"),
        dbt_runner=create_runner(tmp_path),
        approvals=approvals,
        audit=JsonlAuditSink(audit_path),
    )
    actor = ActorContext(
        user_id="engineer-1",
        tenant_id="tenant-1",
        project_id="project-1",
        data_domains=("commerce",),
    )
    task_id = uuid4()
    selected_resources = ("model.orders",)
    approval_request = runtime.request_dbt_test_approval(
        task_id=task_id,
        actor=actor,
        connector_id="warehouse",
        target="ci",
        selected_resources=selected_resources,
        impact_scope="Run tests for model.orders in the pinned CI target only.",
        cost_estimate="Less than one minute of local CI compute.",
        rollback_plan="No production state is changed; discard generated target files.",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    with pytest.raises(PolicyDeniedError, match="require_approval"):
        runtime.gateway.invoke(
            runtime._dbt_test_request(
                task_id=task_id,
                actor=actor,
                connector_id="warehouse",
                target="ci",
                selected_resources=selected_resources,
            ),
            trace_id="dbt-test-without-approval",
        )

    approvals.approve(approval_request.approval_id, approver_id="reviewer-1")
    result = runtime.test_dbt(
        task_id=task_id,
        actor=actor,
        connector_id="warehouse",
        target="ci",
        selected_resources=selected_resources,
        approval_id=approval_request.approval_id,
        trace_id="dbt-test-approved",
    )

    assert result.succeeded is True
    assert result.command[:2] == ("dbt", "test")
    assert str(tmp_path) not in " ".join(result.command)
    assert str(tmp_path) not in result.stdout_tail
    assert str(tmp_path) not in result.stderr_tail
    assert result.production_write_enabled is False
    assert len(list((tmp_path / "artifacts").rglob("*.json"))) == 2
    audit_events = [json.loads(line) for line in audit_path.read_text().splitlines()]
    assert [event["event_type"] for event in audit_events] == [
        "policy.decision",
        "policy.decision",
        "approval.consumed",
    ]
    with pytest.raises(ApprovalError, match="already been consumed"):
        runtime.test_dbt(
            task_id=task_id,
            actor=actor,
            connector_id="warehouse",
            target="ci",
            selected_resources=selected_resources,
            approval_id=approval_request.approval_id,
            trace_id="dbt-test-replay",
        )
