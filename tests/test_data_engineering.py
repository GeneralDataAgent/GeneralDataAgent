import sqlite3
from uuid import uuid4

import duckdb
import pytest

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.connectors import ConnectorError, DuckDBConnector, SQLiteConnector
from generaldataagent.contracts import ActorContext, AgentRole, ToolRequest
from generaldataagent.contracts.data_engineering import (
    ColumnSchema,
    ConnectorCapabilities,
    ProfileBatchRequest,
    QualityRule,
    RelationSchema,
)
from generaldataagent.data_engineering import (
    DataEngineeringRuntime,
    compare_schemas,
    execute_quality_rules,
    profile_relation,
)
from generaldataagent.policy import PolicyEngine


def create_sqlite_database(path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE orders (id INTEGER NOT NULL, channel TEXT, amount REAL)"
        )
        connection.executemany(
            "INSERT INTO orders VALUES (?, ?, ?)",
            [(1, "app", 10.0), (2, "web", 20.0), (3, None, 30.0)],
        )


def create_duckdb_database(path) -> None:
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            "CREATE TABLE orders (id INTEGER NOT NULL, channel VARCHAR, amount DOUBLE)"
        )
        connection.executemany(
            "INSERT INTO orders VALUES (?, ?, ?)",
            [(1, "app", 10.0), (2, "web", 20.0), (3, None, 30.0)],
        )


@pytest.mark.parametrize(
    ("factory", "creator", "expected_type"),
    [
        (SQLiteConnector, create_sqlite_database, "REAL"),
        (DuckDBConnector, create_duckdb_database, "DOUBLE"),
    ],
)
def test_profile_is_consistent_across_connectors(
    tmp_path, factory, creator, expected_type
) -> None:
    database = tmp_path / f"source-{factory.__name__}.db"
    creator(database)
    connector = factory(database)

    profile = profile_relation(connector, "orders")

    assert connector.capabilities.read_only is True
    assert connector.capabilities.dml is False
    assert profile.row_count == 3
    assert [column.name for column in profile.columns] == ["id", "channel", "amount"]
    assert profile.columns[1].null_count == 1
    assert profile.columns[1].distinct_count == 2
    assert profile.columns[2].data_type == expected_type
    assert profile.columns[2].minimum == 10.0
    assert profile.columns[2].maximum == 30.0
    assert profile.columns[2].mean == 20.0


@pytest.mark.parametrize(
    ("factory", "creator"),
    [
        (SQLiteConnector, create_sqlite_database),
        (DuckDBConnector, create_duckdb_database),
    ],
)
def test_connectors_fail_closed_on_writes(tmp_path, factory, creator) -> None:
    database = tmp_path / f"read-only-{factory.__name__}.db"
    creator(database)
    connector = factory(database)

    with pytest.raises(ConnectorError) as caught:
        connector.fetch_all("DELETE FROM orders")

    assert caught.value.code.value == "permission_denied"
    assert caught.value.retryable is False


def test_capabilities_reject_read_only_mutation_claims() -> None:
    with pytest.raises(ValueError, match="read-only"):
        ConnectorCapabilities(
            connector_id="unsafe",
            dialect="test",
            read_only=True,
            schema_introspection=True,
            profiling=True,
            sampling=False,
            explain=False,
            dml=True,
        )


def test_schema_diff_detects_breaking_and_non_breaking_changes() -> None:
    before = RelationSchema(
        connector_id="sqlite",
        relation="orders",
        columns=(
            ColumnSchema(name="id", data_type="INTEGER", nullable=False, ordinal_position=0),
            ColumnSchema(name="amount", data_type="REAL", nullable=True, ordinal_position=1),
            ColumnSchema(name="legacy", data_type="TEXT", nullable=True, ordinal_position=2),
        ),
    )
    after = RelationSchema(
        connector_id="duckdb",
        relation="orders",
        columns=(
            ColumnSchema(name="id", data_type="BIGINT", nullable=False, ordinal_position=0),
            ColumnSchema(name="amount", data_type="REAL", nullable=False, ordinal_position=1),
            ColumnSchema(name="region", data_type="TEXT", nullable=True, ordinal_position=2),
        ),
    )

    result = compare_schemas(before, after)

    assert result.breaking is True
    assert [(item.name, item.change_type) for item in result.changes] == [
        ("legacy", "removed"),
        ("region", "added"),
        ("amount", "nullability_changed"),
        ("id", "type_changed"),
    ]


def test_runtime_routes_profile_through_policy_and_persists_artifact(tmp_path) -> None:
    database = tmp_path / "source.db"
    create_sqlite_database(database)
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    runtime = DataEngineeringRuntime(
        connectors={"warehouse": SQLiteConnector(database, connector_id="warehouse")},
        connector_domains={"warehouse": frozenset({"commerce"})},
        artifacts=artifacts,
    )
    actor = ActorContext(
        user_id="user-1",
        tenant_id="tenant-1",
        project_id="project-1",
        data_domains=("commerce",),
    )

    result = runtime.profile_relation(
        task_id=uuid4(),
        actor=actor,
        connector_id="warehouse",
        relation="orders",
        trace_id="trace-1",
    )

    assert result.row_count == 3
    assert list((tmp_path / "artifacts").rglob("*.json"))


@pytest.mark.parametrize(
    ("factory", "creator"),
    [
        (SQLiteConnector, create_sqlite_database),
        (DuckDBConnector, create_duckdb_database),
    ],
)
def test_quality_rules_find_expected_violations(tmp_path, factory, creator) -> None:
    database = tmp_path / f"quality-{factory.__name__}.db"
    creator(database)
    report = execute_quality_rules(
        factory(database),
        "orders",
        (
            QualityRule(rule_id="id-required", column="id", rule_type="not_null"),
            QualityRule(rule_id="id-unique", column="id", rule_type="unique"),
            QualityRule(
                rule_id="known-channel",
                column="channel",
                rule_type="accepted_values",
                parameters={"values": ["app"]},
                severity="warning",
            ),
            QualityRule(
                rule_id="amount-limit",
                column="amount",
                rule_type="range",
                parameters={"minimum": 15, "maximum": 25},
            ),
        ),
    )

    assert [(check.rule_id, check.violation_count) for check in report.checks] == [
        ("id-required", 0),
        ("id-unique", 0),
        ("known-channel", 1),
        ("amount-limit", 2),
    ]
    assert report.passed is False


def test_runtime_quality_check_is_policy_gated_and_persisted(tmp_path) -> None:
    database = tmp_path / "source.db"
    create_sqlite_database(database)
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

    result = runtime.check_quality(
        task_id=uuid4(),
        actor=actor,
        connector_id="warehouse",
        relation="orders",
        rules=(
            QualityRule(rule_id="channel-required", column="channel", rule_type="not_null"),
        ),
        trace_id="trace-2",
    )

    assert result.checks[0].violation_count == 1
    assert result.passed is False
    assert list((tmp_path / "artifacts").rglob("*.json"))


def test_runtime_rejects_actor_without_connector_domain(tmp_path) -> None:
    database = tmp_path / "source.db"
    create_sqlite_database(database)
    runtime = DataEngineeringRuntime(
        connectors={"warehouse": SQLiteConnector(database, connector_id="warehouse")},
        connector_domains={"warehouse": frozenset({"commerce"})},
        artifacts=LocalArtifactStore(tmp_path / "artifacts"),
    )
    actor = ActorContext(
        user_id="user-1",
        tenant_id="tenant-1",
        project_id="project-1",
        data_domains=("finance",),
    )

    with pytest.raises(PermissionError, match="not authorized"):
        runtime.profile_relation(
            task_id=uuid4(),
            actor=actor,
            connector_id="warehouse",
            relation="orders",
            trace_id="trace-3",
        )


def test_quality_rule_contract_rejects_invalid_parameters() -> None:
    with pytest.raises(ValueError, match="non-empty values"):
        QualityRule(
            rule_id="bad-values",
            column="channel",
            rule_type="accepted_values",
            parameters={"values": []},
        )


def test_policy_rejects_profile_without_data_domain() -> None:
    request = ToolRequest(
        task_id=uuid4(),
        user_id="user-1",
        tenant_id="tenant-1",
        project_id="project-1",
        agent_role=AgentRole.DATA_ENGINEER,
        tool_name="data.profile",
        action="profile",
        resource="warehouse:orders",
        data_domains=(),
    )

    decision = PolicyEngine().evaluate(request)

    assert decision.decision == "deny"
    assert decision.reason_code == "missing_data_domain"


def test_parallel_profiling_isolates_fifty_requests(tmp_path) -> None:
    small_database = tmp_path / "small.db"
    large_database = tmp_path / "large.db"
    create_sqlite_database(small_database)
    with sqlite3.connect(large_database) as connection:
        connection.execute("CREATE TABLE orders (id INTEGER NOT NULL)")
        connection.executemany("INSERT INTO orders VALUES (?)", [(index,) for index in range(7)])
    runtime = DataEngineeringRuntime(
        connectors={
            "small": SQLiteConnector(small_database, connector_id="small"),
            "large": SQLiteConnector(large_database, connector_id="large"),
        },
        connector_domains={
            "small": frozenset({"commerce"}),
            "large": frozenset({"commerce"}),
        },
        artifacts=LocalArtifactStore(tmp_path / "artifacts"),
    )
    actor = ActorContext(
        user_id="user-1",
        tenant_id="tenant-1",
        project_id="project-1",
        data_domains=("commerce",),
    )
    requests = tuple(
        ProfileBatchRequest(
            connector_id="small" if index % 2 == 0 else "large",
            relation="orders",
        )
        for index in range(50)
    )

    result = runtime.profile_relations_parallel(
        actor=actor,
        requests=requests,
        trace_id="parallel-test",
        max_workers=8,
    )

    assert result.all_succeeded is True
    assert [item.request_id for item in result.items] == [item.request_id for item in requests]
    assert [item.profile.row_count for item in result.items if item.profile] == [
        3 if index % 2 == 0 else 7 for index in range(50)
    ]
    assert len({item.task_id for item in result.items}) == 50
    assert len(list((tmp_path / "artifacts").rglob("*.json"))) == 50


def test_parallel_profiling_reports_partial_failure_without_cross_talk(tmp_path) -> None:
    database = tmp_path / "source.db"
    create_sqlite_database(database)
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
    requests = (
        ProfileBatchRequest(connector_id="warehouse", relation="missing"),
        ProfileBatchRequest(connector_id="warehouse", relation="orders"),
    )

    result = runtime.profile_relations_parallel(
        actor=actor,
        requests=requests,
        trace_id="partial-test",
        max_workers=2,
    )

    assert result.all_succeeded is False
    assert result.items[0].status == "failed"
    assert result.items[0].error_code == "not_found"
    assert result.items[1].status == "succeeded"
    assert result.items[1].profile is not None
    assert result.items[1].profile.row_count == 3
