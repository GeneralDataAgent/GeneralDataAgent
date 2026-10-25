"""A read-only operator audit reports corruption without exposing lesson text."""

import importlib.util
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from generaldataagent.business.experience import ExperienceRecord, ExperienceStore
from generaldataagent.business.governance import Feedback, FeedbackRegistry
from generaldataagent.contracts import ActorContext

ACTOR = ActorContext(user_id="u", tenant_id="t", project_id="p", data_domains=("payments",))
NOW = datetime(2026, 10, 24, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]


def record(**changes):
    values = dict(
        failure_class="rule_semantics_excerpt_missing",
        applies_when=("a required excerpt is missing from supplied documents",),
        does_not_apply_when=("the documentation does not establish the semantics",),
        repair="locate a verbatim source passage before retrying",
        counterexample="inventing documentation remains invalid",
        tool_scope=("rules",),
        owner=ACTOR.user_id,
        created_at=NOW,
    )
    values.update(changes)
    return ExperienceRecord(**values)


@pytest.fixture
def stores(tmp_path):
    registry = FeedbackRegistry(tmp_path / "feedback.sqlite")
    item = Feedback(
        task_id=uuid4(),
        result_hash="sha256:" + "a" * 64,
        category="grounding",
        rating=2,
        redacted_question="private question sentinel",
        expected_outcome="private outcome sentinel",
    )
    registry.register_task(item.task_id, ACTOR, item.result_hash)
    registry.submit(item, ACTOR)
    for reviewer in ("quality", "security"):
        registry.review(item.feedback_id, reviewer_id=reviewer, accepted=True)
    dataset_id = uuid4()
    registry.freeze(dataset_id, (item.feedback_id,), split="training")

    store = ExperienceStore(tmp_path / "experience.sqlite", clock=lambda: NOW)
    feedback_record = record()
    store.capture_from_feedback(
        feedback_record,
        ACTOR,
        registry=registry,
        dataset_id=dataset_id,
        feedback_id=item.feedback_id,
    )
    manual_record = record(repair="manual lesson text sentinel")
    store.capture(manual_record, ACTOR)
    second_manual = record(counterexample="second lesson text sentinel")
    store.capture(second_manual, ACTOR)
    return registry, store, feedback_record, manual_record, second_manual


def test_clean_store_reports_origins_and_no_findings(stores):
    _, store, *_ = stores
    report = store.audit()
    assert report["consistent"] is True
    assert report["findings"] == []
    assert report["records"] == 3
    assert report["by_origin"] == {"feedback": 1, "manual": 2}
    assert report["by_state"] == {"candidate": 3}
    assert report["scopes"] == 1


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize(
    "scope", [None, "", "private scope sentinel", "sha256:" + "g" * 64, 42, b"x"]
)
def test_audit_detects_invalid_scope_without_exposing_or_repairing_it(stores, scope, active):
    _, store, _, manual_record, _ = stores
    if active:
        store.review(
            manual_record.experience_id, reviewer_id="reviewer", accepted=True, actor=ACTOR
        )
    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE experiences SET scope=? WHERE id=?", (scope, str(manual_record.experience_id))
        )
    before = store.path.read_bytes()
    reader = ExperienceStore(store.path, clock=lambda: NOW, read_only=True)
    report = reader.audit()
    assert report["consistent"] is False
    assert report["findings"] == [
        {"experience_id": str(manual_record.experience_id), "code": "invalid_scope"}
    ]
    assert report["records"] == 3
    assert report["by_origin"] == {"feedback": 1, "manual": 2}
    assert "sentinel" not in json.dumps(report)
    assert store.path.read_bytes() == before


def test_audit_reports_corrupt_capture_receipts(stores):
    _, store, feedback_record, _, _ = stores
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE experience_feedback_sources SET body='{'")
    report = store.audit()
    assert report["consistent"] is False
    assert report["findings"] == [
        {"experience_id": str(feedback_record.experience_id), "code": "invalid_capture_receipt"}
    ]
    # The record itself is still counted; the audit is not a filter.
    assert report["records"] == 3


def test_audit_reports_unreadable_records(stores):
    _, store, feedback_record, _, _ = stores
    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE experiences SET body='{' WHERE id=?", (str(feedback_record.experience_id),)
        )
    report = store.audit()
    assert report["consistent"] is False
    assert report["findings"] == [
        {"experience_id": str(feedback_record.experience_id), "code": "unreadable_record"}
    ]


def test_audit_reports_origin_mismatch(stores):
    _, store, feedback_record, _, _ = stores
    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE experience_origins SET origin='manual' WHERE id=?",
            (str(feedback_record.experience_id),),
        )
    report = store.audit()
    assert report["findings"] == [
        {"experience_id": str(feedback_record.experience_id), "code": "origin_mismatch"}
    ]


def test_audit_reports_review_evidence_and_state_mismatch(stores):
    _, store, feedback_record, manual_record, _ = stores
    store.review(feedback_record.experience_id, reviewer_id="reviewer", accepted=False, actor=ACTOR)
    store.review(manual_record.experience_id, reviewer_id="reviewer", accepted=True, actor=ACTOR)
    assert store.audit()["by_state"] == {"active": 1, "candidate": 2}
    with sqlite3.connect(store.path) as db:
        # The stored state now claims active while the review rows were removed.
        db.execute("DELETE FROM experience_reviews WHERE id=?", (str(manual_record.experience_id),))
    report = store.audit()
    assert report["findings"] == [
        {"experience_id": str(manual_record.experience_id), "code": "invalid_review_evidence"}
    ]


def test_audit_detects_self_review(stores):
    _, store, _, manual_record, _ = stores
    store.review(manual_record.experience_id, reviewer_id="reviewer", accepted=True, actor=ACTOR)
    with sqlite3.connect(store.path) as db:
        # A self-review row inserted directly, bypassing the store.
        db.execute(
            "INSERT INTO experience_reviews VALUES(?,?,1)",
            (str(manual_record.experience_id), ACTOR.user_id),
        )
    findings = store.audit()["findings"]
    assert findings == [
        {"experience_id": str(manual_record.experience_id), "code": "invalid_review_evidence"}
    ]


def test_audit_is_read_only_and_withholds_lesson_text(stores):
    _, store, feedback_record, manual_record, _ = stores
    before = store.path.read_bytes()
    report = store.audit()
    reader = ExperienceStore(store.path, clock=lambda: NOW, read_only=True)
    assert reader.audit() == report
    assert store.path.read_bytes() == before
    text = json.dumps(report)
    for sentinel in ("sentinel", "verbatim source passage", "inventing documentation", "owner"):
        assert sentinel not in text


def test_audit_names_records_only_when_it_has_a_finding(stores, tmp_path):
    _, store, feedback_record, _, _ = stores
    clean = tmp_path / "clean.json"
    clean.write_text(json.dumps(store.audit()))
    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE experience_origins SET origin='manual' WHERE id=?",
            (str(feedback_record.experience_id),),
        )
    report = store.audit()
    assert str(feedback_record.experience_id) in json.dumps(report)
    assert str(feedback_record.experience_id) not in clean.read_text()


@pytest.mark.parametrize(
    "table,code",
    [
        ("experience_reviews", "orphan_review"),
        ("experience_origins", "orphan_origin"),
        ("experience_feedback_sources", "orphan_feedback_receipt"),
        ("experience_extraction_sources", "orphan_extraction_receipt"),
    ],
)
def test_audit_detects_orphan_associations_without_modifying_the_store(tmp_path, table, code):
    store = ExperienceStore(tmp_path / "orphan.sqlite", clock=lambda: NOW)
    item = record()
    store.capture(item, ACTOR)
    with sqlite3.connect(store.path) as db:
        # Isolate each association so detection cannot depend on another table.
        for association in (
            "experience_reviews",
            "experience_origins",
            "experience_feedback_sources",
            "experience_extraction_sources",
        ):
            db.execute(f"DELETE FROM {association}")
        if table == "experience_reviews":
            db.executemany(
                "INSERT INTO experience_reviews VALUES(?,?,1)",
                [(str(item.experience_id), reviewer) for reviewer in ("quality", "security")],
            )
        elif table == "experience_origins":
            db.execute(
                "INSERT INTO experience_origins VALUES(?,?)", (str(item.experience_id), "manual")
            )
        else:
            db.execute(
                f"INSERT INTO {table} VALUES(?,?,?)",
                (str(item.experience_id), "private orphan receipt sentinel", "unused"),
            )
        db.execute("DELETE FROM experiences")
    before = store.path.read_bytes()
    reader = ExperienceStore(store.path, clock=lambda: NOW, read_only=True)
    report = reader.audit()
    assert report["consistent"] is False
    assert report["records"] == report["scopes"] == 0
    assert report["by_origin"] == report["by_state"] == {}
    assert report["findings"] == [{"experience_id": str(item.experience_id), "code": code}]
    assert "sentinel" not in json.dumps(report)
    assert store.path.read_bytes() == before


def test_audit_handles_a_store_without_provenance_tables(tmp_path):
    store = ExperienceStore(tmp_path / "legacy.sqlite", clock=lambda: NOW)
    store.capture(record(), ACTOR)
    with sqlite3.connect(store.path) as db:
        db.execute("DELETE FROM experience_origins")
    report = store.audit()
    assert report["consistent"] is True
    assert report["by_origin"] == {"manual": 1}


def cli():
    path = ROOT / "evals/audit_stage10_experience.py"
    spec = importlib.util.spec_from_file_location("audit_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_cli(module, monkeypatch, store, output):
    monkeypatch.setattr(module, "ROOT", store.parent)
    monkeypatch.setattr(
        sys,
        "argv",
        ["audit_stage10_experience.py", "--store", str(store), "--output", str(output)],
    )
    module.main()
    return 0


def test_cli_writes_the_report_and_signals_findings(stores, tmp_path, monkeypatch):
    _, store, feedback_record, _, _ = stores
    module = cli()
    output = tmp_path / "audit.json"
    before = store.path.read_bytes()
    assert run_cli(module, monkeypatch, store.path, output) == 0
    report = json.loads(output.read_text())
    assert report["consistent"] is True and report["records"] == 3
    assert store.path.read_bytes() == before

    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE experience_feedback_sources SET body='{'")
    second = tmp_path / "audit-2.json"
    with pytest.raises(SystemExit) as failure:
        run_cli(module, monkeypatch, store.path, second)
    assert failure.value.code == 1
    assert json.loads(second.read_text())["findings"] == [
        {"experience_id": str(feedback_record.experience_id), "code": "invalid_capture_receipt"}
    ]


def test_cli_signals_invalid_scope_in_an_activated_record(stores, tmp_path, monkeypatch):
    _, store, _, manual_record, _ = stores
    store.review(manual_record.experience_id, reviewer_id="reviewer", accepted=True, actor=ACTOR)
    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE experiences SET scope=NULL WHERE id=?", (str(manual_record.experience_id),)
        )
    before = store.path.read_bytes()
    output = tmp_path / "scope-audit.json"
    with pytest.raises(SystemExit) as failure:
        run_cli(cli(), monkeypatch, store.path, output)
    assert failure.value.code == 1
    report = json.loads(output.read_text())
    assert report["consistent"] is False
    assert report["findings"] == [
        {"experience_id": str(manual_record.experience_id), "code": "invalid_scope"}
    ]
    assert store.path.read_bytes() == before


def test_cli_refuses_an_existing_or_outside_output(stores, tmp_path, monkeypatch):
    _, store, *_ = stores
    module = cli()
    existing = tmp_path / "audit.json"
    existing.write_text("{}")
    with pytest.raises(SystemExit) as refusal:
        run_cli(module, monkeypatch, store.path, existing)
    assert refusal.value.code == 2
    assert existing.read_text() == "{}"
    outside = Path("/tmp/audit-outside.json")
    if not outside.is_relative_to(ROOT):
        with pytest.raises(SystemExit) as refusal:
            run_cli(module, monkeypatch, store.path, outside)
        assert refusal.value.code == 2
    assert not outside.exists()
