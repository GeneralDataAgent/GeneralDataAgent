"""Evaluation readers must not initialize or mutate saved experience inputs."""

import hashlib
import io
import json
import sqlite3
from datetime import UTC, datetime

import pytest

from generaldataagent.business.experience import ExperienceRecord, ExperienceStore
from generaldataagent.contracts import ActorContext

ACTOR = ActorContext(user_id="u", tenant_id="t", project_id="p", data_domains=("payments",))


def clock():
    return datetime.now(UTC)


def prepared(path):
    store = ExperienceStore(path, clock=clock)
    item = ExperienceRecord(
        failure_class="unknown_column",
        applies_when=("a supplied column name is absent",),
        does_not_apply_when=("the column exists",),
        repair="inspect the published schema before retrying",
        counterexample="inventing another column remains invalid",
        tool_scope=("sql",),
        owner="u",
        created_at=clock(),
    )
    store.capture(item, ACTOR)
    store.review(item.experience_id, reviewer_id="reviewer", accepted=True, actor=ACTOR)
    return item


def test_readonly_store_retrieves_without_changing_snapshot(tmp_path):
    path = tmp_path / "experience.sqlite"
    item = prepared(path)
    before = path.read_bytes()
    reader = ExperienceStore(path, clock=clock, read_only=True)
    found = reader.retrieve(failure_class="unknown_column", tool="sql", actor=ACTOR)
    assert [row.experience_id for row in found] == [item.experience_id]
    assert reader.state(item.experience_id, ACTOR) == "active"
    assert path.read_bytes() == before


@pytest.mark.parametrize("operation", ["capture", "review"])
def test_readonly_store_refuses_mutation(tmp_path, operation):
    path = tmp_path / "experience.sqlite"
    item = prepared(path)
    before = path.read_bytes()
    reader = ExperienceStore(path, clock=clock, read_only=True)
    with pytest.raises(PermissionError, match="experience_store_read_only"):
        if operation == "capture":
            reader.capture(item.model_copy(update={"repair": "a different repair"}), ACTOR)
        else:
            reader.review(item.experience_id, reviewer_id="another", accepted=False, actor=ACTOR)
    assert path.read_bytes() == before


def test_readonly_missing_store_does_not_create_parent_or_file(tmp_path):
    path = tmp_path / "absent" / "experience.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        ExperienceStore(path, clock=clock, read_only=True)
    assert not path.parent.exists()


def test_readonly_incomplete_schema_is_not_repaired(tmp_path):
    path = tmp_path / "incomplete.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE experiences (id TEXT PRIMARY KEY)")
    before = path.read_bytes()
    with pytest.raises(sqlite3.OperationalError):
        ExperienceStore(path, clock=clock, read_only=True)
    assert path.read_bytes() == before


def test_worker_uses_readonly_experience_reader(tmp_path, monkeypatch, capsys):
    import generaldataagent.evaluation.stage_six_worker as worker

    path = tmp_path / "experience.sqlite"
    prepared(path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    original = ExperienceStore
    opened = []

    class Reader(original):
        def __init__(self, *args, **kwargs):
            opened.append(kwargs.get("read_only", False))
            super().__init__(*args, **kwargs)

    class Agent:
        def __init__(self, model, *, experience, **kwargs):
            self.experience = experience

        def run(self, *args, **kwargs):
            hints = self.experience("unknown_column", "sql")
            assert hints[0]["repair"] == "inspect the published schema before retrying"
            return {"hints": len(hints), "tokens": 0}

    monkeypatch.setattr("generaldataagent.business.experience.ExperienceStore", Reader)
    monkeypatch.setattr(worker, "resolve_evaluation_model", lambda **kwargs: (object(), {}))
    monkeypatch.setattr(worker, "build_evaluation_model", lambda *args: object())
    monkeypatch.setattr(worker, "BoundedSQLAgent", Agent)
    payload = dict(
        app_type="scripted",
        provider_id="scripted",
        experience_database=str(path),
        experience_actor=ACTOR.model_dump(mode="json"),
        database=str(tmp_path / "unused.sqlite"),
        documents="",
        task=dict(
            benchmark="synthetic",
            case_id="readonly",
            question="q",
            instructions="",
            difficulty="easy",
            asset="unused",
        ),
    )
    monkeypatch.setattr(worker.sys, "stdin", io.StringIO(json.dumps(payload)))
    worker.main()
    assert json.loads(capsys.readouterr().out) == {"hints": 1, "tokens": 0}
    assert opened == [True]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
