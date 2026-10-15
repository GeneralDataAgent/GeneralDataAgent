import sqlite3
from uuid import uuid4

import pytest

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.connectors import SQLiteConnector
from generaldataagent.contracts import (
    ActorContext,
    LineageAsset,
    TransformationTest,
)
from generaldataagent.data_engineering import (
    DataEngineeringRuntime,
    analyze_impact,
    build_transformation_proposal,
)
from generaldataagent.tools import SqlGuardError


def test_builds_dry_run_transformation_with_extracted_dependencies() -> None:
    proposal = build_transformation_proposal(
        connector_id="warehouse",
        dialect="sqlite",
        target_relation="daily_orders",
        select_sql=(
            "WITH clean AS (SELECT id, amount FROM orders WHERE amount >= 0) "
            "SELECT COUNT(*) AS order_count FROM clean"
        ),
        allowed_relations={"orders"},
        tests=(
            TransformationTest(
                name="has-rows",
                assertion="row_count_nonzero",
            ),
        ),
        impact_summary="Refreshes the daily aggregate consumed by the sales dashboard.",
        rollback_plan="Keep the current relation and discard this proposal.",
    )

    assert proposal.dependencies == ("orders",)
    assert proposal.dry_run_only is True
    assert "CREATE" not in proposal.select_sql


def test_transformation_test_requires_complete_parameters() -> None:
    with pytest.raises(ValueError, match="non-empty values"):
        TransformationTest(
            name="known-channel",
            assertion="accepted_values",
            column="channel",
        )


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM orders",
        "CREATE TABLE copy AS SELECT * FROM orders",
        "SELECT * FROM restricted_orders",
    ],
)
def test_transformation_proposal_rejects_mutation_and_unauthorized_sources(sql) -> None:
    with pytest.raises(SqlGuardError):
        build_transformation_proposal(
            connector_id="warehouse",
            dialect="sqlite",
            target_relation="daily_orders",
            select_sql=sql,
            allowed_relations={"orders"},
            tests=(TransformationTest(name="has-rows", assertion="row_count_nonzero"),),
            impact_summary="No production change is performed.",
            rollback_plan="Discard the proposal.",
        )


def test_impact_analysis_returns_shortest_deterministic_paths() -> None:
    result = analyze_impact(
        (
            LineageAsset(asset_id="raw.orders", asset_type="source"),
            LineageAsset(
                asset_id="model.orders",
                asset_type="model",
                depends_on=("raw.orders",),
            ),
            LineageAsset(
                asset_id="metric.revenue",
                asset_type="metric",
                depends_on=("model.orders",),
            ),
            LineageAsset(
                asset_id="dashboard.sales",
                asset_type="dashboard",
                depends_on=("metric.revenue", "raw.orders"),
            ),
        ),
        ("raw.orders",),
    )

    assert [(item.asset_id, item.distance, item.path) for item in result.impacted_assets] == [
        ("dashboard.sales", 1, ("raw.orders", "dashboard.sales")),
        ("model.orders", 1, ("raw.orders", "model.orders")),
        (
            "metric.revenue",
            2,
            ("raw.orders", "model.orders", "metric.revenue"),
        ),
    ]
    assert result.has_cycles is False


def test_impact_analysis_flags_cycles_without_looping() -> None:
    result = analyze_impact(
        (
            LineageAsset(asset_id="model.a", asset_type="model", depends_on=("model.b",)),
            LineageAsset(asset_id="model.b", asset_type="model", depends_on=("model.a",)),
        ),
        ("model.a",),
    )

    assert result.has_cycles is True
    assert [item.asset_id for item in result.impacted_assets] == ["model.b"]


def test_runtime_persists_proposal_and_impact_artifacts(tmp_path) -> None:
    database = tmp_path / "source.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE orders (id INTEGER, amount REAL)")
        connection.execute("INSERT INTO orders VALUES (1, 10.0)")
    runtime = DataEngineeringRuntime(
        connectors={"warehouse": SQLiteConnector(database, connector_id="warehouse")},
        connector_domains={"warehouse": frozenset({"commerce"})},
        artifacts=LocalArtifactStore(tmp_path / "artifacts"),
    )
    actor = ActorContext(
        user_id="user-1",
        tenant_id="tenant-1",
        project_id="project-1",
        data_domains=("commerce",),
    )
    task_id = uuid4()
    proposal = runtime.propose_transformation(
        task_id=task_id,
        actor=actor,
        connector_id="warehouse",
        target_relation="daily_orders",
        select_sql="SELECT COUNT(*) AS order_count FROM orders",
        allowed_relations={"orders"},
        tests=(TransformationTest(name="has-rows", assertion="row_count_nonzero"),),
        impact_summary="Adds a draft aggregate.",
        rollback_plan="Discard the draft.",
        trace_id="trace-proposal",
    )
    impact = runtime.analyze_impact(
        task_id=task_id,
        actor=actor,
        connector_ids=("warehouse",),
        assets=(
            LineageAsset(asset_id="orders", asset_type="source"),
            LineageAsset(
                asset_id="daily_orders",
                asset_type="model",
                depends_on=("orders",),
            ),
        ),
        changed_assets=("orders",),
        trace_id="trace-impact",
    )

    assert proposal.dependencies == ("orders",)
    assert impact.impacted_assets[0].asset_id == "daily_orders"
    assert len(list((tmp_path / "artifacts").rglob("*.json"))) == 2
