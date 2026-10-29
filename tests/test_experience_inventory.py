"""Scoped review preparation keeps all source/vote evidence without modifying records."""

import importlib.util
import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from test_stage10_experience import ACTOR, record

from generaldataagent.business.actions import content_hash
from generaldataagent.business.experience import ExperienceError, ExperienceStore

NOW = datetime(2026, 10, 30, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    return ExperienceStore(tmp_path / "experience.sqlite", clock=lambda: NOW)


def members(report):
    return {row["experience_id"]: row for group in report["groups"] for row in group["members"]}


def test_complete_inventory_groups_exact_lessons_and_preserves_record_hashes(store):
    first = record(created_at=NOW)
    second = record(created_at=NOW - timedelta(days=1))
    third = record(repair="核对公开来源后重试", created_at=NOW)
    for item in (first, second, third):
        store.capture(item, ACTOR)
    before = store.path.read_bytes()
    reader = ExperienceStore(store.path, clock=lambda: NOW, read_only=True)
    report = reader.review_inventory(ACTOR)
    assert report["records"] == 3 and report["distinct_lessons"] == 2
    assert report["repeated_lesson_records"] == 1
    assert report["by_state"] == {"candidate": 3}
    assert report["by_review_status"] == {"unreviewed": 3}
    assert report["by_origin"] == {"manual": 3}
    assert report["review_actions_performed"] == 0
    assert report["source_authenticated"] is report["production_accepted"] is False
    rows = members(report)
    for item in (first, second, third):
        assert rows[str(item.experience_id)]["record_hash"] == content_hash(
            item.model_dump(mode="json")
        )
        assert rows[str(item.experience_id)]["source"] is None
        assert (
            rows[str(item.experience_id)]["created_at"]
            == item.model_dump(mode="json")["created_at"]
        )
    body = {key: value for key, value in report.items() if key != "inventory_hash"}
    assert content_hash(body) == report["inventory_hash"]
    assert reader.review_inventory(ACTOR) == report
    assert store.path.read_bytes() == before


def test_group_membership_cannot_transfer_acceptance_or_hide_rejection(store):
    pending, active, rejected = [record() for _ in range(3)]
    for item in (pending, active, rejected):
        store.capture(item, ACTOR)
    store.review(active.experience_id, reviewer_id="reviewer", accepted=True, actor=ACTOR)
    store.review(rejected.experience_id, reviewer_id="reviewer", accepted=False, actor=ACTOR)
    report = store.review_inventory(ACTOR)
    assert report["distinct_lessons"] == 1 and report["groups"][0]["member_count"] == 3
    assert report["by_state"] == {"active": 1, "candidate": 2}
    assert report["by_review_status"] == {"accepted": 1, "rejected": 1, "unreviewed": 1}
    rows = members(report)
    assert rows[str(pending.experience_id)]["review_status"] == "unreviewed"
    assert rows[str(rejected.experience_id)]["reviews"] == {"accepted": 0, "rejected": 1}
    assert store.retrieve(failure_class=active.failure_class, tool="rules", actor=ACTOR) == (
        active,
    )


@pytest.mark.parametrize(
    "change",
    [
        {"repair": "supply merchant and year, then choose exactly one time scope "},
        {"does_not_apply_when": ("different condition",)},
        {"counterexample": "another counterexample"},
        {"tool_scope": ("rules", "sql")},
        {"schema_version": "different-version"},
    ],
)
def test_near_matches_and_other_contract_versions_are_not_equated(store, change):
    store.capture(record(), ACTOR)
    store.capture(record(**change), ACTOR)
    assert store.review_inventory(ACTOR)["distinct_lessons"] == 2


@pytest.mark.parametrize(
    "change",
    [
        {"user_id": "other"},
        {"tenant_id": "other"},
        {"project_id": "other"},
        {"data_domains": ("other",)},
    ],
)
def test_exact_actor_scope_is_required_and_other_scopes_are_not_counted(store, change):
    store.capture(record(), ACTOR)
    actor = ACTOR.model_copy(update=change)
    other = record(owner=actor.user_id, repair="another scope sentinel")
    store.capture(other, actor)
    report = store.review_inventory(ACTOR)
    assert report["records"] == 1 and "sentinel" not in json.dumps(report)
    assert store.review_inventory(actor)["records"] == 1
    assert store.review_inventory(actor)["groups"][0]["group_id"] != report["groups"][0]["group_id"]


@pytest.mark.parametrize("limit", [0, -1, True, 1.0, 1001])
def test_invalid_inventory_limits_are_rejected(store, limit):
    with pytest.raises(ValueError, match="experience_inventory_invalid_limit"):
        store.review_inventory(ACTOR, max_records=limit)


def test_over_limit_fails_instead_of_reporting_a_partial_group_count(store):
    for _ in range(2):
        store.capture(record(), ACTOR)
    with pytest.raises(ValueError, match="experience_inventory_limit_exceeded"):
        store.review_inventory(ACTOR, max_records=1)
    assert store.review_inventory(ACTOR, max_records=2)["records"] == 2


@pytest.mark.parametrize("corruption", ["body", "owner", "origin", "state"])
def test_any_corrupt_scoped_member_refuses_the_whole_inventory(store, corruption):
    first, second = record(), record()
    for item in (first, second):
        store.capture(item, ACTOR)
    with sqlite3.connect(store.path) as db:
        if corruption == "origin":
            db.execute("DELETE FROM experience_origins WHERE id=?", (str(second.experience_id),))
        elif corruption == "state":
            db.execute(
                "UPDATE experiences SET state='active' WHERE id=?", (str(second.experience_id),)
            )
        else:
            changed = (
                second.model_copy(update={"owner": "other"}) if corruption == "owner" else second
            )
            digest = (
                content_hash(changed.model_dump(mode="json")) if corruption == "owner" else "bad"
            )
            db.execute(
                "UPDATE experiences SET body=?,hash=? WHERE id=?",
                (changed.model_dump_json(), digest, str(second.experience_id)),
            )
    with pytest.raises(ExperienceError, match="experience_integrity_failure"):
        store.review_inventory(ACTOR)


def test_inventory_holds_one_snapshot_across_concurrent_capture(store, monkeypatch):
    initial, later = record(), record()
    store.capture(initial, ACTOR)
    with sqlite3.connect(store.path) as db:
        db.execute("PRAGMA journal_mode=WAL")
    original = store._load
    calls = []

    def concurrent(db, identifier, actor):
        if not calls:
            calls.append(identifier)
            store.capture(later, ACTOR)
        return original(db, identifier, actor)

    monkeypatch.setattr(store, "_load", concurrent)
    report = store.review_inventory(ACTOR)
    assert report["records"] == 1 and list(members(report)) == [str(initial.experience_id)]
    assert store.review_inventory(ACTOR)["records"] == 2


def test_sql_sources_stay_separate_even_when_the_lesson_is_identical(tmp_path, store):
    from test_sql_experience_extraction import BAD_SQL, scripted_trace

    from generaldataagent.experience.extraction import extract_experiences

    trace = scripted_trace(tmp_path, [BAD_SQL["unknown_column"], "SELECT amount FROM observations"])
    (candidate,) = extract_experiences(trace, documents="", owner=ACTOR.user_id, created_at=NOW)
    store.capture_from_extraction(
        candidate,
        ACTOR,
        trace_bytes=json.dumps(trace).encode(),
        document_bytes=b"",
        extractor_hash="a" * 64,
    )
    store.capture(candidate.record.model_copy(update={"experience_id": uuid4()}), ACTOR)
    report = store.review_inventory(ACTOR)
    assert report["records"] == 2 and report["distinct_lessons"] == 1
    assert report["by_origin"] == {"extraction": 1, "manual": 1}
    source = members(report)[str(candidate.record.experience_id)]["source"]
    assert source == store.extraction_source(candidate.record.experience_id, ACTOR)
    assert "SELECT" not in json.dumps(report)
    assert store.retrieve(failure_class="unknown_column", tool="sql", actor=ACTOR) == ()


def test_feedback_inventory_contains_receipt_references_without_training_labels(tmp_path):
    from test_stage10_experience_feedback import sources

    registry, feedback, store, item = sources.__wrapped__(tmp_path)
    dataset_id = uuid4()
    registry.freeze(dataset_id, (feedback.feedback_id,), split="training")
    store.capture_from_feedback(
        item, ACTOR, registry=registry, dataset_id=dataset_id, feedback_id=feedback.feedback_id
    )
    report = store.review_inventory(ACTOR)
    assert report["by_origin"] == {"feedback": 1}
    source = members(report)[str(item.experience_id)]["source"]
    assert source["dataset_id"] == str(dataset_id) and source["split"] == "training"
    assert "private question" not in json.dumps(report)
    assert "private outcome" not in json.dumps(report)


def test_cli_is_read_only_and_refuses_overwrite(store, tmp_path, monkeypatch, capsys):
    store.capture(record(), ACTOR)
    path = Path(__file__).resolve().parents[1] / "evals/inventory_stage10_experience.py"
    spec = importlib.util.spec_from_file_location("inventory_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    output = tmp_path / "inventory.json"
    before = store.path.read_bytes()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "inventory",
            "--store",
            str(store.path),
            "--actor",
            ACTOR.model_dump_json(),
            "--output",
            str(output),
        ],
    )
    module.main()
    report = json.loads(output.read_text())
    assert report["records"] == 1 and json.loads(capsys.readouterr().out)["records"] == 1
    assert store.path.read_bytes() == before
    with pytest.raises(SystemExit):
        module.main()
