"""Persisted project/creator access, legacy isolation, and real runtime integration."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from test_execution_composition import _metric_fixture, _metric_query

from generaldataagent.artifacts.governed import (
    ArtifactReadPolicy,
    ArtifactScopeError,
    GovernedArtifactStore,
    require_artifact_scope,
)
from generaldataagent.artifacts.local import LocalArtifactStore
from generaldataagent.contracts import ActorContext, ArtifactRef
from generaldataagent.observability import JsonlAuditSink

ALICE = ActorContext(user_id="alice", tenant_id="t", project_id="p", data_domains=("payments",))
BOB = ALICE.model_copy(update={"user_id": "bob"})
NOW = datetime(2026, 10, 30, tzinfo=UTC)


def store(root, *, actor=ALICE, policy="project", clock=lambda: NOW):
    return GovernedArtifactStore(
        root / "content",
        index_path=root / "index.sqlite",
        actor=actor,
        read_policy=policy,
        clock=clock,
        retention_seconds=10,
    )


def put(value):
    return value.put_json(
        task_id=uuid4(), artifact_type="report", created_by="reporter", value={"count": 2}
    )


def legacy(root):
    content = LocalArtifactStore(root / "content")
    ref = put(content)
    raw_ref = json.dumps(ref.model_dump(mode="json"), indent=1)
    with sqlite3.connect(root / "index.sqlite") as db:
        db.execute(
            "CREATE TABLE governed_artifacts (artifact_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, "
            "tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, domains TEXT NOT NULL, "
            "ref_json TEXT NOT NULL, expires_at REAL NOT NULL, held INTEGER NOT NULL DEFAULT 0, "
            "deleted INTEGER NOT NULL DEFAULT 0)"
        )
        db.execute(
            "INSERT INTO governed_artifacts VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0)",
            (
                str(ref.artifact_id),
                str(ref.task_id),
                "t",
                "p",
                '["payments"]',
                raw_ref,
                (NOW + timedelta(seconds=10)).timestamp(),
            ),
        )
    return ref


@pytest.mark.parametrize("reader_policy", list(ArtifactReadPolicy))
def test_project_sharing_uses_the_persisted_policy_and_each_readers_identity(
    tmp_path, reader_policy
):
    writer = store(tmp_path)
    ref = put(writer)
    reader = store(tmp_path, actor=BOB, policy=reader_policy)
    require_artifact_scope(reader, BOB)
    assert reader.read_bytes(ref) == writer.read_bytes(ref)
    assert reader.export_bytes(ref) == b'{"count":2}'
    with sqlite3.connect(writer.index_path) as db:
        assert db.execute(
            "SELECT read_policy, creator_user_id FROM governed_artifacts_v2"
        ).fetchall() == [("project", "alice")]


@pytest.mark.parametrize("reader_policy", list(ArtifactReadPolicy))
def test_private_records_cannot_be_reopened_as_shared(tmp_path, reader_policy):
    ref = put(store(tmp_path, policy="creator"))
    assert store(tmp_path, policy=reader_policy).read_bytes(ref) == b'{"count":2}'
    stranger = store(tmp_path, actor=BOB, policy=reader_policy)
    for operation in (stranger.read_bytes, stranger.export_bytes, stranger.discard):
        with pytest.raises(PermissionError, match="artifact_access_denied"):
            operation(ref)
    with pytest.raises(PermissionError, match="artifact_access_denied"):
        stranger.set_legal_hold(ref, held=True)
    assert store(tmp_path).read_bytes(ref) == b'{"count":2}'


@pytest.mark.parametrize("policy", list(ArtifactReadPolicy))
@pytest.mark.parametrize(
    "change", [{"tenant_id": "other"}, {"project_id": "other"}, {"data_domains": ()}]
)
def test_scope_constraints_apply_to_both_policies(tmp_path, policy, change):
    ref = put(store(tmp_path, policy=policy))
    with pytest.raises(PermissionError, match="artifact_access_denied"):
        store(tmp_path, actor=ALICE.model_copy(update=change)).read_bytes(ref)


def test_legacy_rows_keep_their_bytes_and_project_sharing_without_inferred_owners(tmp_path):
    ref = legacy(tmp_path)
    with sqlite3.connect(tmp_path / "index.sqlite") as db:
        before = db.execute("SELECT * FROM governed_artifacts").fetchall()
    alice, bob = store(tmp_path, policy="creator"), store(tmp_path, actor=BOB, policy="creator")
    assert bob.read_bytes(ref) == b'{"count":2}'
    with sqlite3.connect(alice.index_path) as db:
        assert db.execute("SELECT * FROM governed_artifacts").fetchall() == before
        assert db.execute("SELECT COUNT(*) FROM governed_artifacts_v2").fetchone()[0] == 0
    bob.set_legal_hold(ref, held=True)
    with pytest.raises(PermissionError, match="hold"):
        alice.discard(ref)
    bob.set_legal_hold(ref, held=False)
    assert alice.discard(ref)
    with pytest.raises(PermissionError, match="expired_or_deleted"):
        bob.read_bytes(ref)


def test_mixed_layout_retention_respects_private_owners_and_holds(tmp_path):
    old = legacy(tmp_path)
    now = NOW
    alice = store(tmp_path, policy="creator", clock=lambda: now)
    private, held = put(alice), put(alice)
    shared = put(store(tmp_path, clock=lambda: now))
    alice.set_legal_hold(held, held=True)
    now += timedelta(seconds=10)
    bob = store(tmp_path, actor=BOB, clock=lambda: now)
    assert set(bob.delete_expired()) == {old.artifact_id, shared.artifact_id}
    assert alice._path(private).exists() and alice._path(held).exists()
    assert alice.delete_expired() == (private.artifact_id,)
    alice.set_legal_hold(held, held=False)
    assert alice.delete_expired() == (held.artifact_id,)
    assert alice.delete_expired() == bob.delete_expired() == ()


@pytest.mark.parametrize("policy", [None, "unknown", "", True, 1])
def test_invalid_policy_is_rejected_before_creating_storage(tmp_path, policy):
    with pytest.raises(ValueError, match="read policy"):
        store(tmp_path, policy=policy)
    assert not list(tmp_path.iterdir())


def test_unknown_creator_identity_is_not_recorded(tmp_path):
    with pytest.raises(ValueError, match="creator identity"):
        store(tmp_path, actor=ALICE.model_copy(update={"user_id": " "}))
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "field,value",
    [
        ("read_policy", "unknown"),
        ("read_policy", ""),
        ("creator_user_id", ""),
        ("creator_user_id", " "),
    ],
)
def test_unknown_policy_or_missing_owner_fails_closed(tmp_path, field, value):
    own = store(tmp_path, policy="creator")
    ref = put(own)
    assert field in {"read_policy", "creator_user_id"}
    with sqlite3.connect(own.index_path) as db:
        db.execute(f"UPDATE governed_artifacts_v2 SET {field} = ?", (value,))
    with pytest.raises(PermissionError, match="artifact_access_denied"):
        own.read_bytes(ref)


def test_duplicate_identity_does_not_fall_back_to_legacy_sharing(tmp_path):
    own = store(tmp_path, policy="creator")
    ref = put(own)
    with sqlite3.connect(own.index_path) as db:
        db.execute(
            "INSERT INTO governed_artifacts SELECT artifact_id, task_id, tenant_id, project_id, "
            "domains, ref_json, expires_at, held, deleted FROM governed_artifacts_v2"
        )
    for who in (ALICE, BOB):
        with pytest.raises(PermissionError, match="artifact_access_denied"):
            store(tmp_path, actor=who).read_bytes(ref)


def test_partial_access_layout_is_not_silently_repaired(tmp_path):
    index = tmp_path / "index.sqlite"
    with sqlite3.connect(index) as db:
        db.execute("CREATE TABLE governed_artifacts_v2 (artifact_id TEXT PRIMARY KEY)")
    with pytest.raises(ValueError, match="index layout"):
        store(tmp_path)
    with sqlite3.connect(index) as db:
        assert (
            db.execute("PRAGMA table_info(governed_artifacts_v2)").fetchall()[0][1] == "artifact_id"
        )
        assert (
            db.execute("SELECT name FROM sqlite_master WHERE name='governed_artifacts'").fetchall()
            == []
        )


def test_new_index_write_failure_removes_the_unregistered_content(tmp_path):
    own = store(tmp_path, policy="creator")
    with sqlite3.connect(own.index_path) as db:
        db.execute(
            "CREATE TRIGGER refuse_insert BEFORE INSERT ON governed_artifacts_v2 "
            "BEGIN SELECT RAISE(ABORT, 'fixture unavailable'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        put(own)
    assert not list(own.root.rglob("*.json"))


@pytest.mark.parametrize("policy", list(ArtifactReadPolicy))
def test_real_metric_workflow_writes_and_checks_delivery_under_both_policies(tmp_path, policy):
    own = store(tmp_path, policy=policy)
    runtime = _metric_fixture(
        tmp_path, artifacts=own, audit=JsonlAuditSink(tmp_path / "audit.jsonl"), budgets=None
    )
    result = runtime.run(_metric_query(), ALICE)
    assert result["status"] == "completed" and result["validation_result"]["status"] == "pass"
    references = [
        ArtifactRef.model_validate(value) for value in result["delivery_package"]["evidence"]
    ]
    assert references
    reader = store(tmp_path, actor=BOB)
    for ref in references:
        assert own.read_bytes(ref)
        if policy == ArtifactReadPolicy.CREATOR:
            with pytest.raises(PermissionError):
                reader.read_bytes(ref)
        else:
            assert reader.read_bytes(ref) == own.read_bytes(ref)
    with pytest.raises(ArtifactScopeError):
        runtime.run(_metric_query(), BOB)
