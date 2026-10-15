"""Qualify the local dbt adapter without claiming a real-project result."""

import json
import sqlite3
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.connectors import SQLiteConnector
from generaldataagent.contracts import ActorContext
from generaldataagent.data_engineering import (
    DataEngineeringRuntime,
    DbtCompileError,
    DbtCompileRunner,
)
from generaldataagent.policy import ApprovalError, InMemoryApprovalStore
from generaldataagent.tools import PolicyDeniedError


def run_dbt_adapter_evaluation() -> dict[str, Any]:
    checks: list[dict[str, str]] = []
    with TemporaryDirectory(prefix="generaldataagent-dbt-adapter-") as directory:
        root = Path(directory)
        runner = _create_runner(root)
        compile_result = runner.compile(target="ci")
        _record(checks, "compile_success", compile_result.succeeded)
        selected_result = runner.compile(
            target="ci",
            selected_resources=("model.orders+",),
        )
        _record(
            checks,
            "compile_selector_preserved",
            selected_result.selected_resources == ("model.orders+",),
        )
        failed_result = runner.compile(
            target="ci",
            selected_resources=("fail.model",),
        )
        _record(
            checks,
            "compile_nonzero_exit_reported",
            failed_result.exit_code == 3 and not failed_result.succeeded,
        )
        _expect_error(
            checks,
            "production_target_rejected",
            PermissionError,
            "not approved",
            lambda: runner.compile(target="prod"),
        )
        _expect_error(
            checks,
            "unsafe_selector_rejected",
            ValueError,
            "invalid dbt selectors",
            lambda: runner.compile(target="ci", selected_resources=("model.orders;drop",)),
        )
        _expect_error(
            checks,
            "compile_timeout_enforced",
            DbtCompileError,
            "exceeded",
            lambda: _timeout_runner(runner).compile(
                target="ci", selected_resources=("slow.model",)
            ),
        )
        serialized_report = selected_result.model_dump_json()
        _record(
            checks,
            "command_paths_redacted",
            str(root) not in " ".join(selected_result.command),
        )
        _record(checks, "output_paths_redacted", str(root) not in serialized_report)
        _expect_error(
            checks,
            "test_requires_selection",
            ValueError,
            "at least one selected resource",
            lambda: runner.validate_test_request(target="ci", selected_resources=()),
        )
        _expect_error(
            checks,
            "test_production_target_rejected",
            PermissionError,
            "not approved",
            lambda: runner.validate_test_request(
                target="prod",
                selected_resources=("model.orders",),
            ),
        )
        _expect_error(
            checks,
            "test_unsafe_selector_rejected",
            ValueError,
            "invalid dbt selectors",
            lambda: runner.validate_test_request(
                target="ci",
                selected_resources=("model.orders;drop",),
            ),
        )
        runtime, actor, approvals = _create_runtime(root, runner)
        task_id = uuid4()
        _expect_error(
            checks,
            "test_without_approval_blocked",
            PolicyDeniedError,
            "require_approval",
            lambda: runtime.test_dbt(
                task_id=task_id,
                actor=actor,
                connector_id="warehouse",
                target="ci",
                selected_resources=("model.orders",),
                approval_id=None,
                trace_id="dbt-unapproved",
            ),
        )
        approval = runtime.request_dbt_test_approval(
            task_id=task_id,
            actor=actor,
            connector_id="warehouse",
            target="ci",
            selected_resources=("model.orders",),
            impact_scope="Run selected local adapter tests only.",
            cost_estimate="Less than one minute of local compute.",
            rollback_plan="Discard generated target files.",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        approvals.approve(approval.approval_id, approver_id="reviewer")
        _expect_error(
            checks,
            "changed_request_rejected",
            ApprovalError,
            "does not match final request",
            lambda: runtime.test_dbt(
                task_id=task_id,
                actor=actor,
                connector_id="warehouse",
                target="ci",
                selected_resources=("model.changed",),
                approval_id=approval.approval_id,
                trace_id="dbt-changed",
            ),
        )
        test_result = runtime.test_dbt(
            task_id=task_id,
            actor=actor,
            connector_id="warehouse",
            target="ci",
            selected_resources=("model.orders",),
            approval_id=approval.approval_id,
            trace_id="dbt-approved",
        )
        _record(checks, "approved_selected_test_succeeds", test_result.succeeded)
        _expect_error(
            checks,
            "approval_replay_rejected",
            ApprovalError,
            "already been consumed",
            lambda: runtime.test_dbt(
                task_id=task_id,
                actor=actor,
                connector_id="warehouse",
                target="ci",
                selected_resources=("model.orders",),
                approval_id=approval.approval_id,
                trace_id="dbt-replay",
            ),
        )
        failure_task = uuid4()
        failure_approval = runtime.request_dbt_test_approval(
            task_id=failure_task,
            actor=actor,
            connector_id="warehouse",
            target="ci",
            selected_resources=("fail.model",),
            impact_scope="Exercise a local nonzero dbt test exit.",
            cost_estimate="Less than one minute of local compute.",
            rollback_plan="Discard generated target files.",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        approvals.approve(failure_approval.approval_id, approver_id="reviewer")
        failed_test = runtime.test_dbt(
            task_id=failure_task,
            actor=actor,
            connector_id="warehouse",
            target="ci",
            selected_resources=("fail.model",),
            approval_id=failure_approval.approval_id,
            trace_id="dbt-failed",
        )
        _record(
            checks,
            "test_nonzero_exit_reported",
            failed_test.exit_code == 3 and not failed_test.succeeded,
        )
    failed = [check for check in checks if check["status"] != "passed"]
    return {
        "check_count": len(checks),
        "passed": len(checks) - len(failed),
        "failed": len(failed),
        "checks": checks,
    }


def _create_runner(root: Path) -> DbtCompileRunner:
    project = root / "project"
    profiles = root / "profiles"
    project.mkdir()
    profiles.mkdir()
    (project / "dbt_project.yml").write_text("name: qualification\nversion: 1.0.0\n")
    script = root / "fake_dbt.py"
    script.write_text(
        "import sys, time\n"
        "args = sys.argv[1:]\n"
        "print('stdout ' + ' '.join(args))\n"
        "print('stderr ' + ' '.join(args), file=sys.stderr)\n"
        "time.sleep(0.2 if 'slow.model' in args else 0)\n"
        "raise SystemExit(3 if 'fail.model' in args else 0)\n"
    )
    return DbtCompileRunner(
        project_dir=project,
        profiles_dir=profiles,
        executable=(sys.executable, str(script)),
        allowed_targets=frozenset({"ci"}),
        timeout_seconds=1.0,
    )


def _timeout_runner(runner: DbtCompileRunner) -> DbtCompileRunner:
    return DbtCompileRunner(
        project_dir=runner.project_dir,
        profiles_dir=runner.profiles_dir,
        executable=runner.executable,
        allowed_targets=runner.allowed_targets,
        timeout_seconds=0.05,
        environment=runner.environment,
    )


def _create_runtime(
    root: Path,
    runner: DbtCompileRunner,
) -> tuple[DataEngineeringRuntime, ActorContext, InMemoryApprovalStore]:
    database = root / "source.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE orders (id INTEGER)")
    approvals = InMemoryApprovalStore()
    runtime = DataEngineeringRuntime(
        connectors={"warehouse": SQLiteConnector(database, connector_id="warehouse")},
        connector_domains={"warehouse": frozenset({"commerce"})},
        artifacts=LocalArtifactStore(root / "artifacts"),
        dbt_runner=runner,
        approvals=approvals,
    )
    actor = ActorContext(
        user_id="engineer",
        tenant_id="local",
        project_id="stage2",
        data_domains=("commerce",),
    )
    return runtime, actor, approvals


def _record(checks: list[dict[str, str]], check_id: str, passed: bool) -> None:
    checks.append({"check_id": check_id, "status": "passed" if passed else "failed"})


def _expect_error(
    checks: list[dict[str, str]],
    check_id: str,
    error_type: type[Exception],
    message: str,
    operation: Callable[[], object],
) -> None:
    try:
        operation()
    except error_type as exc:
        _record(checks, check_id, message in str(exc))
    except Exception:
        _record(checks, check_id, False)
    else:
        _record(checks, check_id, False)


if __name__ == "__main__":
    result = run_dbt_adapter_evaluation()
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0 if result["failed"] == 0 else 1)
