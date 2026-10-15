import sqlite3
from uuid import uuid4

import pytest

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.connectors import SQLiteConnector
from generaldataagent.contracts import (
    ActorContext,
    ColumnProfile,
    ColumnSchema,
    DataProfile,
    RelationSchema,
)
from generaldataagent.data_engineering import DataEngineeringRuntime, propose_quality_rules


def schema() -> RelationSchema:
    return RelationSchema(
        connector_id="warehouse",
        relation="orders",
        columns=(
            ColumnSchema(name="order_id", data_type="INTEGER", nullable=False, ordinal_position=0),
            ColumnSchema(name="amount", data_type="REAL", nullable=True, ordinal_position=1),
            ColumnSchema(name="channel", data_type="TEXT", nullable=True, ordinal_position=2),
        ),
    )


def profile() -> DataProfile:
    return DataProfile(
        connector_id="warehouse",
        relation="orders",
        row_count=3,
        columns=(
            ColumnProfile(
                name="order_id",
                data_type="INTEGER",
                row_count=3,
                null_count=0,
                distinct_count=3,
                minimum=1,
                maximum=3,
                mean=2,
            ),
            ColumnProfile(
                name="amount",
                data_type="REAL",
                row_count=3,
                null_count=1,
                distinct_count=2,
                minimum=10.0,
                maximum=20.0,
                mean=15.0,
            ),
            ColumnProfile(
                name="channel",
                data_type="TEXT",
                row_count=3,
                null_count=0,
                distinct_count=2,
                minimum="app",
                maximum="web",
            ),
        ),
    )


def test_proposes_conservative_schema_and_profile_rules() -> None:
    proposal = propose_quality_rules(schema(), profile())

    assert [rule.rule_type for rule in proposal.rules] == [
        "not_null",
        "unique",
        "range",
        "range",
    ]
    assert [rule.severity for rule in proposal.rules] == [
        "error",
        "warning",
        "warning",
        "warning",
    ]
    assert proposal.rules[2].parameters == {"minimum": 1, "maximum": 3}
    assert proposal.rules[3].parameters == {"minimum": 10.0, "maximum": 20.0}
    assert len({rule.rule_id for rule in proposal.rules}) == 4


def test_rejects_mismatched_or_unproductive_evidence() -> None:
    mismatched = profile().model_copy(update={"relation": "customers"})
    with pytest.raises(ValueError, match="same relation"):
        propose_quality_rules(schema(), mismatched)

    empty_schema = RelationSchema(
        connector_id="warehouse",
        relation="labels",
        columns=(ColumnSchema(name="label", data_type="TEXT", nullable=True, ordinal_position=0),),
    )
    empty_profile = DataProfile(
        connector_id="warehouse",
        relation="labels",
        row_count=0,
        columns=(
            ColumnProfile(
                name="label",
                data_type="TEXT",
                row_count=0,
                null_count=0,
                distinct_count=0,
            ),
        ),
    )
    with pytest.raises(ValueError, match="no evidence-backed"):
        propose_quality_rules(empty_schema, empty_profile)


def test_runtime_policy_gates_and_persists_quality_rule_proposal(tmp_path) -> None:
    database = tmp_path / "source.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE orders (order_id INTEGER NOT NULL)")
        connection.executemany("INSERT INTO orders VALUES (?)", [(1,), (2,), (3,)])
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
    relation_schema = runtime.inspect_relation(
        task_id=uuid4(),
        actor=actor,
        connector_id="warehouse",
        relation="orders",
        trace_id="quality-schema",
    )
    relation_profile = runtime.profile_relation(
        task_id=uuid4(),
        actor=actor,
        connector_id="warehouse",
        relation="orders",
        trace_id="quality-profile",
    )

    proposal = runtime.propose_quality_rules(
        task_id=task_id,
        actor=actor,
        schema=relation_schema,
        profile=relation_profile,
        trace_id="quality-proposal",
    )

    assert [rule.rule_type for rule in proposal.rules] == ["not_null", "unique", "range"]
    artifact_paths = list((tmp_path / "artifacts" / str(task_id)).glob("*.json"))
    assert len(artifact_paths) == 1
