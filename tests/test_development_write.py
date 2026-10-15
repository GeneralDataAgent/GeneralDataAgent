import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.connectors import SQLiteConnector
from generaldataagent.contracts import ActorContext, TransformationProposal, TransformationTest
from generaldataagent.data_engineering import (
    DataEngineeringRuntime,
    DevelopmentWriteError,
    SQLiteDevelopmentWriteRunner,
    build_transformation_proposal,
)
from generaldataagent.policy import ApprovalError, InMemoryApprovalStore, SQLiteApprovalStore
from generaldataagent.tools import PolicyDeniedError


def create_database(path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE orders (id INTEGER NOT NULL, channel TEXT)")
        connection.executemany(
            "INSERT INTO orders VALUES (?, ?)",
            [(1, "app"), (2, "web"), (3, "app")],
        )


def proposal(
    *,
    target: str = "dev_daily_orders",
    select_sql: str = "SELECT channel, COUNT(*) AS order_count FROM orders GROUP BY channel",
    tests: tuple[TransformationTest, ...] | None = None,
) -> TransformationProposal:
    return build_transformation_proposal(
        connector_id="warehouse",
        dialect="sqlite",
        target_relation=target,
        select_sql=select_sql,
        allowed_relations={"orders"},
        tests=tests or (TransformationTest(name="has-rows", assertion="row_count_nonzero"),),
        impact_summary="Creates one isolated development aggregate.",
        rollback_plan="Drop the isolated development table.",
    )


def actor() -> ActorContext:
    return ActorContext(
        user_id="requester",
        tenant_id="tenant-1",
        project_id="project-1",
        data_domains=("commerce",),
    )


def runtime(
    tmp_path,
    database,
    approvals=None,
    *,
    timeout_seconds: float = 30.0,
    max_rows: int = 100_000,
) -> DataEngineeringRuntime:
    return DataEngineeringRuntime(
        connectors={"warehouse": SQLiteConnector(database, connector_id="warehouse")},
        connector_domains={"warehouse": frozenset({"commerce"})},
        artifacts=LocalArtifactStore(tmp_path / "artifacts"),
        approvals=approvals,
        development_write_runner=SQLiteDevelopmentWriteRunner(
            database,
            connector_id="warehouse",
            environment="development",
            timeout_seconds=timeout_seconds,
            max_rows=max_rows,
        ),
    )


def test_development_write_is_disabled_unless_explicitly_configured(tmp_path) -> None:
    database = tmp_path / "source.sqlite"
    create_database(database)
    disabled = DataEngineeringRuntime(
        connectors={"warehouse": SQLiteConnector(database, connector_id="warehouse")},
        connector_domains={"warehouse": frozenset({"commerce"})},
        artifacts=LocalArtifactStore(tmp_path / "artifacts"),
    )

    with pytest.raises(RuntimeError, match="not configured"):
        disabled.materialize_development(
            task_id=uuid4(),
            actor=actor(),
            proposal=proposal(),
            approval_id=None,
            trace_id="disabled",
        )


def test_runner_rejects_production_unsafe_and_forged_proposals(tmp_path) -> None:
    database = tmp_path / "source.sqlite"
    create_database(database)
    with pytest.raises(ValueError, match="development or ci"):
        SQLiteDevelopmentWriteRunner(
            database,
            connector_id="warehouse",
            environment="production",  # type: ignore[arg-type]
        )
    runner = SQLiteDevelopmentWriteRunner(
        database,
        connector_id="warehouse",
        environment="ci",
    )
    with pytest.raises(ValueError, match="configured prefix"):
        runner.validate(proposal(target="daily_orders"))
    forged = TransformationProposal(
        connector_id="warehouse",
        target_relation="dev_bad",
        dialect="sqlite",
        select_sql="DELETE FROM orders",
        dependencies=("orders",),
        tests=(TransformationTest(name="has-rows", assertion="row_count_nonzero"),),
        impact_summary="Forged proposal.",
        rollback_plan="No rollback.",
    )
    with pytest.raises(Exception, match="read-only|SELECT|DELETE"):
        runner.validate(forged)


def test_runtime_rejects_write_runner_bound_to_another_database(tmp_path) -> None:
    source = tmp_path / "source.sqlite"
    other = tmp_path / "other.sqlite"
    create_database(source)
    create_database(other)

    with pytest.raises(ValueError, match="share the registered SQLite connector path"):
        DataEngineeringRuntime(
            connectors={"warehouse": SQLiteConnector(source, connector_id="warehouse")},
            connector_domains={"warehouse": frozenset({"commerce"})},
            artifacts=LocalArtifactStore(tmp_path / "artifacts"),
            development_write_runner=SQLiteDevelopmentWriteRunner(
                other,
                connector_id="warehouse",
                environment="development",
            ),
        )


def test_exact_one_time_approval_materializes_without_overwrite(tmp_path) -> None:
    database = tmp_path / "source.sqlite"
    create_database(database)
    approvals = InMemoryApprovalStore()
    governed = runtime(tmp_path, database, approvals)
    task_id = uuid4()
    change = proposal()

    with pytest.raises(PolicyDeniedError, match="require_approval"):
        governed.materialize_development(
            task_id=task_id,
            actor=actor(),
            proposal=change,
            approval_id=None,
            trace_id="unapproved",
        )

    approval = governed.request_development_write_approval(
        task_id=task_id,
        actor=actor(),
        proposal=change,
        impact_scope="Creates dev_daily_orders in the pinned local development database.",
        cost_estimate="Bounded local SQLite materialization.",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    assert approval.rollback_plan == 'DROP TABLE "dev_daily_orders"'
    approvals.approve(approval.approval_id, approver_id="reviewer")

    report = governed.materialize_development(
        task_id=task_id,
        actor=actor(),
        proposal=change,
        approval_id=approval.approval_id,
        trace_id="approved",
    )

    assert report.succeeded is True
    assert report.production_write_enabled is False
    assert report.environment == "development"
    assert report.rows_written == 2
    assert [(test.name, test.violation_count) for test in report.tests] == [("has-rows", 0)]
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT channel, order_count FROM dev_daily_orders ORDER BY channel"
        ).fetchall()
    assert rows == [("app", 2), ("web", 1)]
    assert len(list((tmp_path / "artifacts" / str(task_id)).glob("*.json"))) == 2

    with pytest.raises(DevelopmentWriteError, match="already exists"):
        governed.request_development_write_approval(
            task_id=uuid4(),
            actor=actor(),
            proposal=change,
            impact_scope="Would overwrite an existing object.",
            cost_estimate="Not applicable.",
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    with pytest.raises((ApprovalError, DevelopmentWriteError)):
        governed.materialize_development(
            task_id=task_id,
            actor=actor(),
            proposal=change,
            approval_id=approval.approval_id,
            trace_id="replay",
        )


def test_failed_materialization_tests_roll_back_and_consume_approval(tmp_path) -> None:
    database = tmp_path / "source.sqlite"
    create_database(database)
    approvals = InMemoryApprovalStore()
    governed = runtime(tmp_path, database, approvals)
    task_id = uuid4()
    empty = proposal(
        target="dev_empty_orders",
        select_sql="SELECT * FROM orders WHERE id < 0",
    )
    approval = governed.request_development_write_approval(
        task_id=task_id,
        actor=actor(),
        proposal=empty,
        impact_scope="Exercise transactional validation rollback.",
        cost_estimate="Bounded local SQLite materialization.",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    approvals.approve(approval.approval_id, approver_id="reviewer")

    with pytest.raises(DevelopmentWriteError, match="validation failed"):
        governed.materialize_development(
            task_id=task_id,
            actor=actor(),
            proposal=empty,
            approval_id=approval.approval_id,
            trace_id="failed-validation",
        )

    with sqlite3.connect(database) as connection:
        target = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'dev_empty_orders'"
        ).fetchone()
    assert target is None
    with pytest.raises(ApprovalError, match="already been consumed"):
        governed.materialize_development(
            task_id=task_id,
            actor=actor(),
            proposal=empty,
            approval_id=approval.approval_id,
            trace_id="failed-validation-replay",
        )

    artifacts = [
        json.loads(path.read_text())
        for path in (tmp_path / "artifacts" / str(task_id)).glob("*.json")
    ]
    failures = [artifact for artifact in artifacts if "error_code" in artifact]
    assert len(artifacts) == 2
    assert len(failures) == 1
    assert failures[0]["error_code"] == "validation_failed"
    assert failures[0]["created_target_persisted"] is False
    assert failures[0]["production_write_enabled"] is False


def test_durable_approval_executes_once_across_runtime_restarts(tmp_path) -> None:
    database = tmp_path / "source.sqlite"
    approval_database = tmp_path / "approvals.sqlite"
    create_database(database)
    task_id = uuid4()
    change = proposal(target="dev_restart_orders")
    initial = runtime(tmp_path, database, SQLiteApprovalStore(approval_database))
    approval = initial.request_development_write_approval(
        task_id=task_id,
        actor=actor(),
        proposal=change,
        impact_scope="Exercise restart-safe development materialization.",
        cost_estimate="Bounded local SQLite materialization.",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    SQLiteApprovalStore(approval_database).approve(
        approval.approval_id,
        approver_id="reviewer",
    )

    restarted = runtime(tmp_path, database, SQLiteApprovalStore(approval_database))
    report = restarted.materialize_development(
        task_id=task_id,
        actor=actor(),
        proposal=change,
        approval_id=approval.approval_id,
        trace_id="after-restart",
    )
    assert report.rows_written == 2

    restarted_again = runtime(tmp_path, database, SQLiteApprovalStore(approval_database))
    with pytest.raises(ApprovalError, match="already been consumed"):
        restarted_again.materialize_development(
            task_id=task_id,
            actor=actor(),
            proposal=change,
            approval_id=approval.approval_id,
            trace_id="after-second-restart",
        )


def test_execution_failure_rolls_back_persists_evidence_and_consumes_approval(tmp_path) -> None:
    database = tmp_path / "source.sqlite"
    create_database(database)
    approvals = InMemoryApprovalStore()
    governed = runtime(tmp_path, database, approvals)
    task_id = uuid4()
    change = proposal(target="dev_missing_source")
    approval = governed.request_development_write_approval(
        task_id=task_id,
        actor=actor(),
        proposal=change,
        impact_scope="Exercise source drift after approval.",
        cost_estimate="Bounded local SQLite materialization.",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    approvals.approve(approval.approval_id, approver_id="reviewer")
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE orders")

    with pytest.raises(DevelopmentWriteError, match="materialization failed") as error:
        governed.materialize_development(
            task_id=task_id,
            actor=actor(),
            proposal=change,
            approval_id=approval.approval_id,
            trace_id="missing-source",
        )
    assert error.value.code == "execution_failed"

    with sqlite3.connect(database) as connection:
        target = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'dev_missing_source'"
        ).fetchone()
    assert target is None
    artifacts = [
        json.loads(path.read_text())
        for path in (tmp_path / "artifacts" / str(task_id)).glob("*.json")
    ]
    failures = [artifact for artifact in artifacts if "error_code" in artifact]
    assert len(failures) == 1
    assert failures[0]["error_code"] == "execution_failed"
    assert failures[0]["created_target_persisted"] is False
    with pytest.raises(ApprovalError, match="already been consumed"):
        governed.materialize_development(
            task_id=task_id,
            actor=actor(),
            proposal=change,
            approval_id=approval.approval_id,
            trace_id="missing-source-replay",
        )


def test_timeout_rolls_back_persists_evidence_and_consumes_approval(tmp_path) -> None:
    database = tmp_path / "source.sqlite"
    create_database(database)
    approvals = InMemoryApprovalStore()
    governed = runtime(tmp_path, database, approvals, timeout_seconds=0.001)
    task_id = uuid4()
    change = proposal(
        target="dev_timeout_orders",
        select_sql=(
            "WITH RECURSIVE counter AS ("
            "SELECT 1 AS value UNION ALL "
            "SELECT value + 1 AS value FROM counter WHERE value < 100000000"
            ") SELECT SUM(value) AS total FROM counter CROSS JOIN orders"
        ),
    )
    approval = governed.request_development_write_approval(
        task_id=task_id,
        actor=actor(),
        proposal=change,
        impact_scope="Exercise bounded development materialization.",
        cost_estimate="One millisecond local execution deadline.",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    approvals.approve(approval.approval_id, approver_id="reviewer")

    with pytest.raises(DevelopmentWriteError, match="exceeded") as error:
        governed.materialize_development(
            task_id=task_id,
            actor=actor(),
            proposal=change,
            approval_id=approval.approval_id,
            trace_id="timeout",
        )
    assert error.value.code == "timeout"

    with sqlite3.connect(database) as connection:
        target = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'dev_timeout_orders'"
        ).fetchone()
    assert target is None
    artifacts = [
        json.loads(path.read_text())
        for path in (tmp_path / "artifacts" / str(task_id)).glob("*.json")
    ]
    failures = [artifact for artifact in artifacts if "error_code" in artifact]
    assert len(failures) == 1
    assert failures[0]["error_code"] == "timeout"
    assert failures[0]["created_target_persisted"] is False
    with pytest.raises(ApprovalError, match="already been consumed"):
        governed.materialize_development(
            task_id=task_id,
            actor=actor(),
            proposal=change,
            approval_id=approval.approval_id,
            trace_id="timeout-replay",
        )


def test_concurrent_approved_writes_create_target_exactly_once(tmp_path) -> None:
    database = tmp_path / "source.sqlite"
    create_database(database)
    approvals = InMemoryApprovalStore()
    governed = runtime(tmp_path, database, approvals)
    attempts = []
    for index in range(2):
        task_id = uuid4()
        change = proposal(target="dev_concurrent_orders")
        approval = governed.request_development_write_approval(
            task_id=task_id,
            actor=actor(),
            proposal=change,
            impact_scope="Exercise concurrent target creation.",
            cost_estimate="Bounded local SQLite materialization.",
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        approvals.approve(approval.approval_id, approver_id=f"reviewer-{index}")
        attempts.append((task_id, change, approval.approval_id))

    def execute(attempt):
        task_id, change, approval_id = attempt
        try:
            result = governed.materialize_development(
                task_id=task_id,
                actor=actor(),
                proposal=change,
                approval_id=approval_id,
                trace_id=f"concurrent-{task_id}",
            )
        except DevelopmentWriteError as exc:
            return task_id, exc
        return task_id, result

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(execute, attempts))

    successes = [
        (task_id, result) for task_id, result in outcomes if not isinstance(result, Exception)
    ]
    failures = [
        (task_id, result)
        for task_id, result in outcomes
        if isinstance(result, DevelopmentWriteError)
    ]
    assert len(successes) == 1
    assert len(failures) == 1
    assert successes[0][1].rows_written == 2
    assert failures[0][1].code == "target_exists"

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT channel, order_count FROM dev_concurrent_orders ORDER BY channel"
        ).fetchall()
    assert rows == [("app", 2), ("web", 1)]

    successful_artifacts = list((tmp_path / "artifacts" / str(successes[0][0])).glob("*.json"))
    failed_artifacts = [
        json.loads(path.read_text())
        for path in (tmp_path / "artifacts" / str(failures[0][0])).glob("*.json")
    ]
    assert len(successful_artifacts) == 2
    assert len(failed_artifacts) == 2
    assert [item["error_code"] for item in failed_artifacts if "error_code" in item] == [
        "target_exists"
    ]
    for task_id, change, approval_id in attempts:
        with pytest.raises(ApprovalError, match="already been consumed"):
            governed.materialize_development(
                task_id=task_id,
                actor=actor(),
                proposal=change,
                approval_id=approval_id,
                trace_id=f"concurrent-replay-{task_id}",
            )


def test_row_budget_rolls_back_persists_evidence_and_consumes_approval(tmp_path) -> None:
    database = tmp_path / "source.sqlite"
    create_database(database)
    with pytest.raises(ValueError, match="max_rows must be positive"):
        SQLiteDevelopmentWriteRunner(
            database,
            connector_id="warehouse",
            environment="development",
            max_rows=0,
        )

    approvals = InMemoryApprovalStore()
    governed = runtime(tmp_path, database, approvals, max_rows=2)
    task_id = uuid4()
    change = proposal(
        target="dev_oversized_orders",
        select_sql="SELECT * FROM orders",
    )
    approval = governed.request_development_write_approval(
        task_id=task_id,
        actor=actor(),
        proposal=change,
        impact_scope="Exercise the development row budget.",
        cost_estimate="At most two materialized rows.",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    approvals.approve(approval.approval_id, approver_id="reviewer")

    with pytest.raises(DevelopmentWriteError, match="configured limit") as error:
        governed.materialize_development(
            task_id=task_id,
            actor=actor(),
            proposal=change,
            approval_id=approval.approval_id,
            trace_id="row-budget",
        )
    assert error.value.code == "row_limit_exceeded"

    with sqlite3.connect(database) as connection:
        target = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'dev_oversized_orders'"
        ).fetchone()
    assert target is None
    artifacts = [
        json.loads(path.read_text())
        for path in (tmp_path / "artifacts" / str(task_id)).glob("*.json")
    ]
    failures = [artifact for artifact in artifacts if "error_code" in artifact]
    assert len(failures) == 1
    assert failures[0]["error_code"] == "row_limit_exceeded"
    assert failures[0]["created_target_persisted"] is False
    with pytest.raises(ApprovalError, match="already been consumed"):
        governed.materialize_development(
            task_id=task_id,
            actor=actor(),
            proposal=change,
            approval_id=approval.approval_id,
            trace_id="row-budget-replay",
        )
