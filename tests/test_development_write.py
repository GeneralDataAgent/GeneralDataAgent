import sqlite3
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
from generaldataagent.policy import ApprovalError, InMemoryApprovalStore
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
        tests=tests
        or (TransformationTest(name="has-rows", assertion="row_count_nonzero"),),
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


def runtime(tmp_path, database, approvals=None) -> DataEngineeringRuntime:
    return DataEngineeringRuntime(
        connectors={"warehouse": SQLiteConnector(database, connector_id="warehouse")},
        connector_domains={"warehouse": frozenset({"commerce"})},
        artifacts=LocalArtifactStore(tmp_path / "artifacts"),
        approvals=approvals,
        development_write_runner=SQLiteDevelopmentWriteRunner(
            database,
            connector_id="warehouse",
            environment="development",
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
