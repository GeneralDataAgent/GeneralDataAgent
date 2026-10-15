"""Qualify the explicitly enabled Stage 2 development-write pilot."""

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.connectors import SQLiteConnector
from generaldataagent.contracts import ActorContext, TransformationTest
from generaldataagent.data_engineering import (
    DataEngineeringRuntime,
    DevelopmentWriteError,
    SQLiteDevelopmentWriteRunner,
    build_transformation_proposal,
)
from generaldataagent.policy import (
    ApprovalError,
    ApprovalStore,
    InMemoryApprovalStore,
    SQLiteApprovalStore,
)
from generaldataagent.tools import PolicyDeniedError


def run_development_write_evaluation() -> dict[str, Any]:
    checks: list[dict[str, str]] = []
    with TemporaryDirectory(prefix="generaldataagent-development-write-") as directory:
        root = Path(directory)
        database = root / "development.sqlite"
        _create_database(database)
        _expect_error(
            checks,
            "production_environment_rejected",
            ValueError,
            "development or ci",
            lambda: SQLiteDevelopmentWriteRunner(
                database,
                connector_id="warehouse",
                environment="production",  # type: ignore[arg-type]
            ),
        )
        runner = SQLiteDevelopmentWriteRunner(
            database,
            connector_id="warehouse",
            environment="development",
        )
        other_database = root / "other.sqlite"
        _create_database(other_database)
        _expect_error(
            checks,
            "connector_path_mismatch_rejected",
            ValueError,
            "share the registered SQLite connector path",
            lambda: _runtime(
                root / "mismatched",
                database,
                approvals=InMemoryApprovalStore(),
                runner=SQLiteDevelopmentWriteRunner(
                    other_database,
                    connector_id="warehouse",
                    environment="development",
                ),
            ),
        )
        _expect_error(
            checks,
            "unsafe_target_rejected",
            ValueError,
            "configured prefix",
            lambda: runner.validate(_proposal(target="daily_orders")),
        )
        approvals = InMemoryApprovalStore()
        actor = _actor()
        disabled = _runtime(root / "disabled", database, approvals=approvals)
        _expect_error(
            checks,
            "default_disabled",
            RuntimeError,
            "not configured",
            lambda: disabled.materialize_development(
                task_id=uuid4(),
                actor=actor,
                proposal=_proposal(),
                approval_id=None,
                trace_id="disabled",
            ),
        )
        runtime = _runtime(root / "enabled", database, approvals=approvals, runner=runner)
        task_id = uuid4()
        proposal = _proposal()
        _expect_error(
            checks,
            "unapproved_write_blocked",
            PolicyDeniedError,
            "require_approval",
            lambda: runtime.materialize_development(
                task_id=task_id,
                actor=actor,
                proposal=proposal,
                approval_id=None,
                trace_id="unapproved",
            ),
        )
        approval = runtime.request_development_write_approval(
            task_id=task_id,
            actor=actor,
            proposal=proposal,
            impact_scope="Create one isolated development table.",
            cost_estimate="Bounded local SQLite execution.",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        _record(
            checks,
            "rollback_is_generated",
            approval.rollback_plan == 'DROP TABLE "dev_daily_orders"',
        )
        _expect_error(
            checks,
            "self_approval_rejected",
            ApprovalError,
            "cannot approve",
            lambda: approvals.approve(approval.approval_id, approver_id=actor.user_id),
        )
        approvals.approve(approval.approval_id, approver_id="reviewer")
        _expect_error(
            checks,
            "changed_request_rejected",
            ApprovalError,
            "does not match final request",
            lambda: runtime.materialize_development(
                task_id=task_id,
                actor=actor,
                proposal=_proposal(select_sql="SELECT * FROM orders WHERE id > 1"),
                approval_id=approval.approval_id,
                trace_id="changed",
            ),
        )
        result = runtime.materialize_development(
            task_id=task_id,
            actor=actor,
            proposal=proposal,
            approval_id=approval.approval_id,
            trace_id="approved",
        )
        _record(checks, "approved_write_succeeds", result.succeeded)
        _record(checks, "row_count_reported", result.rows_written == 2)
        _record(
            checks,
            "validation_tests_reported",
            [(test.name, test.violation_count) for test in result.tests] == [("has-rows", 0)],
        )
        _record(checks, "production_flag_false", not result.production_write_enabled)
        with sqlite3.connect(database) as connection:
            materialized = connection.execute(
                "SELECT channel, order_count FROM dev_daily_orders ORDER BY channel"
            ).fetchall()
            source_count = int(connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0])
            production_target = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'daily_orders'"
            ).fetchone()
        _record(checks, "materialized_values_correct", materialized == [("app", 2), ("web", 1)])
        _record(checks, "source_unchanged", source_count == 3)
        _record(checks, "production_target_absent", production_target is None)
        _record(
            checks,
            "approval_and_report_persisted",
            len(list((root / "enabled" / "artifacts" / str(task_id)).glob("*.json"))) == 2,
        )
        _expect_error(
            checks,
            "approval_replay_rejected",
            ApprovalError,
            "already been consumed",
            lambda: runtime.materialize_development(
                task_id=task_id,
                actor=actor,
                proposal=proposal,
                approval_id=approval.approval_id,
                trace_id="replay",
            ),
        )
        _expect_error(
            checks,
            "existing_target_not_overwritten",
            DevelopmentWriteError,
            "already exists",
            lambda: runtime.request_development_write_approval(
                task_id=uuid4(),
                actor=actor,
                proposal=proposal,
                impact_scope="Attempt duplicate target.",
                cost_estimate="Not applicable.",
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            ),
        )
        failed_task = uuid4()
        empty = _proposal(
            target="dev_empty_orders",
            select_sql="SELECT * FROM orders WHERE id < 0",
        )
        failed_approval = runtime.request_development_write_approval(
            task_id=failed_task,
            actor=actor,
            proposal=empty,
            impact_scope="Exercise transactional test rollback.",
            cost_estimate="Bounded local SQLite execution.",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        approvals.approve(failed_approval.approval_id, approver_id="reviewer")
        _expect_error(
            checks,
            "failed_test_rejects_write",
            DevelopmentWriteError,
            "validation failed",
            lambda: runtime.materialize_development(
                task_id=failed_task,
                actor=actor,
                proposal=empty,
                approval_id=failed_approval.approval_id,
                trace_id="failed-validation",
            ),
        )
        with sqlite3.connect(database) as connection:
            failed_target = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'dev_empty_orders'"
            ).fetchone()
        _record(checks, "failed_test_rolls_back_target", failed_target is None)
        failed_artifacts = [
            json.loads(path.read_text())
            for path in (root / "enabled" / "artifacts" / str(failed_task)).glob("*.json")
        ]
        failure_evidence = [item for item in failed_artifacts if "error_code" in item]
        _record(
            checks,
            "failed_write_evidence_persisted",
            len(failed_artifacts) == 2
            and len(failure_evidence) == 1
            and failure_evidence[0]["error_code"] == "validation_failed"
            and failure_evidence[0]["created_target_persisted"] is False
            and failure_evidence[0]["production_write_enabled"] is False,
        )
        _expect_error(
            checks,
            "failed_write_approval_consumed",
            ApprovalError,
            "already been consumed",
            lambda: runtime.materialize_development(
                task_id=failed_task,
                actor=actor,
                proposal=empty,
                approval_id=failed_approval.approval_id,
                trace_id="failed-validation-replay",
            ),
        )
        durable_root = root / "durable"
        approval_database = durable_root / "approvals.sqlite"
        durable_store = SQLiteApprovalStore(approval_database)
        durable_runtime = _runtime(
            durable_root,
            database,
            approvals=durable_store,
            runner=runner,
        )
        durable_task = uuid4()
        durable_proposal = _proposal(target="dev_restart_orders")
        durable_approval = durable_runtime.request_development_write_approval(
            task_id=durable_task,
            actor=actor,
            proposal=durable_proposal,
            impact_scope="Exercise restart-safe development materialization.",
            cost_estimate="Bounded local SQLite execution.",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        durable_store.approve(durable_approval.approval_id, approver_id="reviewer")
        restarted_runtime = _runtime(
            durable_root,
            database,
            approvals=SQLiteApprovalStore(approval_database),
            runner=runner,
        )
        restarted_report = restarted_runtime.materialize_development(
            task_id=durable_task,
            actor=actor,
            proposal=durable_proposal,
            approval_id=durable_approval.approval_id,
            trace_id="after-restart",
        )
        _record(
            checks,
            "durable_approval_executes_after_restart",
            restarted_report.rows_written == 2,
        )
        restarted_again = _runtime(
            durable_root,
            database,
            approvals=SQLiteApprovalStore(approval_database),
            runner=runner,
        )
        _expect_error(
            checks,
            "durable_approval_replay_blocked_after_restart",
            ApprovalError,
            "already been consumed",
            lambda: restarted_again.materialize_development(
                task_id=durable_task,
                actor=actor,
                proposal=durable_proposal,
                approval_id=durable_approval.approval_id,
                trace_id="after-second-restart",
            ),
        )
        drift_database = root / "drift.sqlite"
        _create_database(drift_database)
        drift_approvals = InMemoryApprovalStore()
        drift_runner = SQLiteDevelopmentWriteRunner(
            drift_database,
            connector_id="warehouse",
            environment="development",
        )
        drift_root = root / "drift"
        drift_runtime = _runtime(
            drift_root,
            drift_database,
            approvals=drift_approvals,
            runner=drift_runner,
        )
        drift_task = uuid4()
        drift_proposal = _proposal(target="dev_missing_source")
        drift_approval = drift_runtime.request_development_write_approval(
            task_id=drift_task,
            actor=actor,
            proposal=drift_proposal,
            impact_scope="Exercise source drift after approval.",
            cost_estimate="Bounded local SQLite execution.",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        drift_approvals.approve(drift_approval.approval_id, approver_id="reviewer")
        with sqlite3.connect(drift_database) as connection:
            connection.execute("DROP TABLE orders")
        _expect_error(
            checks,
            "execution_failure_classified",
            DevelopmentWriteError,
            "materialization failed",
            lambda: drift_runtime.materialize_development(
                task_id=drift_task,
                actor=actor,
                proposal=drift_proposal,
                approval_id=drift_approval.approval_id,
                trace_id="missing-source",
            ),
        )
        with sqlite3.connect(drift_database) as connection:
            drift_target = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'dev_missing_source'"
            ).fetchone()
        _record(checks, "execution_failure_rolls_back_target", drift_target is None)
        drift_artifacts = [
            json.loads(path.read_text())
            for path in (drift_root / "artifacts" / str(drift_task)).glob("*.json")
        ]
        drift_failures = [item for item in drift_artifacts if "error_code" in item]
        _record(
            checks,
            "execution_failure_evidence_persisted",
            len(drift_failures) == 1
            and drift_failures[0]["error_code"] == "execution_failed"
            and drift_failures[0]["created_target_persisted"] is False,
        )
        _expect_error(
            checks,
            "execution_failure_approval_consumed",
            ApprovalError,
            "already been consumed",
            lambda: drift_runtime.materialize_development(
                task_id=drift_task,
                actor=actor,
                proposal=drift_proposal,
                approval_id=drift_approval.approval_id,
                trace_id="missing-source-replay",
            ),
        )
        timeout_database = root / "timeout.sqlite"
        _create_database(timeout_database)
        timeout_approvals = InMemoryApprovalStore()
        timeout_runner = SQLiteDevelopmentWriteRunner(
            timeout_database,
            connector_id="warehouse",
            environment="development",
            timeout_seconds=0.001,
        )
        timeout_root = root / "timeout"
        timeout_runtime = _runtime(
            timeout_root,
            timeout_database,
            approvals=timeout_approvals,
            runner=timeout_runner,
        )
        timeout_task = uuid4()
        timeout_proposal = _proposal(
            target="dev_timeout_orders",
            select_sql=(
                "WITH RECURSIVE counter AS ("
                "SELECT 1 AS value UNION ALL "
                "SELECT value + 1 AS value FROM counter WHERE value < 100000000"
                ") SELECT SUM(value) AS total FROM counter CROSS JOIN orders"
            ),
        )
        timeout_approval = timeout_runtime.request_development_write_approval(
            task_id=timeout_task,
            actor=actor,
            proposal=timeout_proposal,
            impact_scope="Exercise bounded development materialization.",
            cost_estimate="One millisecond local execution deadline.",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        timeout_approvals.approve(timeout_approval.approval_id, approver_id="reviewer")
        _expect_error(
            checks,
            "timeout_classified",
            DevelopmentWriteError,
            "exceeded",
            lambda: timeout_runtime.materialize_development(
                task_id=timeout_task,
                actor=actor,
                proposal=timeout_proposal,
                approval_id=timeout_approval.approval_id,
                trace_id="timeout",
            ),
        )
        with sqlite3.connect(timeout_database) as connection:
            timeout_target = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'dev_timeout_orders'"
            ).fetchone()
        _record(checks, "timeout_rolls_back_target", timeout_target is None)
        timeout_artifacts = [
            json.loads(path.read_text())
            for path in (timeout_root / "artifacts" / str(timeout_task)).glob("*.json")
        ]
        timeout_failures = [item for item in timeout_artifacts if "error_code" in item]
        _record(
            checks,
            "timeout_evidence_persisted",
            len(timeout_failures) == 1
            and timeout_failures[0]["error_code"] == "timeout"
            and timeout_failures[0]["created_target_persisted"] is False,
        )
        _expect_error(
            checks,
            "timeout_approval_consumed",
            ApprovalError,
            "already been consumed",
            lambda: timeout_runtime.materialize_development(
                task_id=timeout_task,
                actor=actor,
                proposal=timeout_proposal,
                approval_id=timeout_approval.approval_id,
                trace_id="timeout-replay",
            ),
        )
    failed = [check for check in checks if check["status"] != "passed"]
    return {
        "check_count": len(checks),
        "passed": len(checks) - len(failed),
        "failed": len(failed),
        "checks": checks,
    }


def _create_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE orders (id INTEGER NOT NULL, channel TEXT)")
        connection.executemany(
            "INSERT INTO orders VALUES (?, ?)",
            [(1, "app"), (2, "web"), (3, "app")],
        )


def _proposal(
    *,
    target: str = "dev_daily_orders",
    select_sql: str = "SELECT channel, COUNT(*) AS order_count FROM orders GROUP BY channel",
):
    return build_transformation_proposal(
        connector_id="warehouse",
        dialect="sqlite",
        target_relation=target,
        select_sql=select_sql,
        allowed_relations={"orders"},
        tests=(TransformationTest(name="has-rows", assertion="row_count_nonzero"),),
        impact_summary="Create one isolated development aggregate.",
        rollback_plan="Drop the isolated development table.",
    )


def _actor() -> ActorContext:
    return ActorContext(
        user_id="engineer",
        tenant_id="local",
        project_id="stage2",
        data_domains=("commerce",),
    )


def _runtime(
    root: Path,
    database: Path,
    *,
    approvals: ApprovalStore,
    runner: SQLiteDevelopmentWriteRunner | None = None,
) -> DataEngineeringRuntime:
    return DataEngineeringRuntime(
        connectors={"warehouse": SQLiteConnector(database, connector_id="warehouse")},
        connector_domains={"warehouse": frozenset({"commerce"})},
        artifacts=LocalArtifactStore(root / "artifacts"),
        approvals=approvals,
        development_write_runner=runner,
    )


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
    result = run_development_write_evaluation()
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0 if result["failed"] == 0 else 1)
