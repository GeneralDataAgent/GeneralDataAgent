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
from generaldataagent.policy import ApprovalError, InMemoryApprovalStore
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
    approvals: InMemoryApprovalStore,
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
