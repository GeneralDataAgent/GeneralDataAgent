"""Versioned opening and explicit copy migration preserve evidence and reviews."""

import hashlib
import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from test_stage10_experience import ACTOR, record
from test_stage10_experience_extraction import (
    DOCUMENTS,
    NOW,
    capture_extracted,
    extract,
)
from test_stage10_experience_extraction import recovered as recovered_trace
from test_stage10_experience_feedback import capture
from test_stage10_experience_feedback import sources as feedback_sources

from generaldataagent.business.actions import content_hash
from generaldataagent.business.experience import ExperienceStore
from generaldataagent.experience.migrations import migrate_legacy_store
from generaldataagent.experience.schema import (
    LEGACY_LAYOUTS,
    TABLES,
    ExperienceSchemaError,
)


@pytest.fixture
def recovered(tmp_path):
    return recovered_trace.__wrapped__(tmp_path)


@pytest.fixture
def sources(tmp_path):
    return feedback_sources.__wrapped__(tmp_path)


def legacy(path, layout):
    """Declare exactly the historical table set; never use this on real inputs."""
    with sqlite3.connect(path) as db:
        for table in set(TABLES) - LEGACY_LAYOUTS[layout]:
            db.execute(f"DROP TABLE {table}")


def rows(path, tables):
    with sqlite3.connect(path) as db:
        return {
            table: db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() for table in tables
        }


@pytest.mark.parametrize("layout", LEGACY_LAYOUTS)
def test_manual_legacy_copy_preserves_all_existing_rows_and_review_gates(tmp_path, layout):
    path, output = tmp_path / "legacy.sqlite", tmp_path / "versioned.sqlite"
    store = ExperienceStore(path, clock=lambda: NOW)
    active, candidate, rejected = (record() for _ in range(3))
    for item in (active, candidate, rejected):
        store.capture(item, ACTOR)
    store.review(active.experience_id, reviewer_id="independent", accepted=True, actor=ACTOR)
    store.review(rejected.experience_id, reviewer_id="independent", accepted=False, actor=ACTOR)
    legacy(path, layout)
    before, original = path.read_bytes(), rows(path, LEGACY_LAYOUTS[layout])
    for read_only in (True, False):
        with pytest.raises(ExperienceSchemaError, match="experience_migration_required"):
            ExperienceStore(path, clock=lambda: NOW, read_only=read_only)
    report = migrate_legacy_store(path, output, legacy_layout=layout)
    assert path.read_bytes() == before
    assert rows(output, LEGACY_LAYOUTS[layout]) == original
    assert report["by_state"] == {"active": 1, "candidate": 2}
    assert report["source_authenticated"] is False
    assert report["output_hash"] == hashlib.sha256(output.read_bytes()).hexdigest()
    reader = ExperienceStore(output, clock=lambda: NOW, read_only=True)
    assert reader.retrieve(failure_class=active.failure_class, tool="rules", actor=ACTOR) == (
        active,
    )
    assert reader.state(candidate.experience_id, ACTOR) == "candidate"
    assert reader.audit()["consistent"] is True
    migrated = output.read_bytes()
    ExperienceStore(output, clock=lambda: NOW)
    assert output.read_bytes() == migrated


@pytest.mark.parametrize("layout", ["feedback-v0", "origins-v0", "complete-v0"])
def test_feedback_migration_preserves_receipts_and_requires_registry_for_approval(sources, layout):
    registry, feedback, store, item = sources
    dataset_id = uuid4()
    registry.freeze(dataset_id, (feedback.feedback_id,), split="training")
    capture(sources, dataset_id)
    receipt = store.feedback_source(item.experience_id, ACTOR)
    legacy(store.path, layout)
    output = store.path.with_name("migrated.sqlite")
    before = store.path.read_bytes()
    report = migrate_legacy_store(store.path, output, legacy_layout=layout)
    migrated = ExperienceStore(output, clock=lambda: NOW)
    assert report["by_origin"] == {"feedback": 1}
    assert migrated.feedback_source(item.experience_id, ACTOR) == receipt
    with pytest.raises(ValueError, match="experience_feedback_source_denied"):
        migrated.review(item.experience_id, reviewer_id="reviewer", accepted=True, actor=ACTOR)
    assert migrated.state(item.experience_id, ACTOR) == "candidate"
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("layout", ["receipts-v0", "complete-v0"])
def test_extraction_migration_preserves_source_bytes_and_keeps_candidate(
    recovered, tmp_path, layout
):
    (candidate,) = extract(recovered)
    store = ExperienceStore(tmp_path / "legacy.sqlite", clock=lambda: NOW)
    capture_extracted(store, candidate, ACTOR, json.dumps(recovered).encode(), DOCUMENTS.encode())
    receipt = store.extraction_source(candidate.record.experience_id, ACTOR)
    legacy(store.path, layout)
    original = rows(store.path, LEGACY_LAYOUTS[layout])
    output = tmp_path / "migrated.sqlite"
    report = migrate_legacy_store(store.path, output, legacy_layout=layout)
    assert rows(output, LEGACY_LAYOUTS[layout]) == original
    assert report["by_origin"] == {"extraction": 1}
    migrated = ExperienceStore(output, clock=lambda: NOW, read_only=True)
    assert migrated.extraction_source(candidate.record.experience_id, ACTOR) == receipt
    assert (
        migrated.retrieve(failure_class=candidate.record.failure_class, tool="rules", actor=ACTOR)
        == ()
    )


@pytest.mark.parametrize("read_only", [True, False])
@pytest.mark.parametrize("version", [0, 2, 999])
def test_unknown_schema_version_refuses_without_rewrite(tmp_path, read_only, version):
    path = tmp_path / "future.sqlite"
    ExperienceStore(path, clock=lambda: NOW)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE experience_schema SET version=?", (version,))
    before = path.read_bytes()
    with pytest.raises(ExperienceSchemaError, match="experience_schema_unsupported"):
        ExperienceStore(path, clock=lambda: NOW, read_only=read_only)
    assert path.read_bytes() == before


@pytest.mark.parametrize("damage", ["table", "column", "metadata", "source", "view"])
def test_schema_damage_refuses_on_existing_and_new_handles(tmp_path, damage):
    path = tmp_path / "current.sqlite"
    store = ExperienceStore(path, clock=lambda: NOW)
    with sqlite3.connect(path) as db:
        if damage == "table":
            db.execute("DROP TABLE experience_origins")
        elif damage == "column":
            db.execute("ALTER TABLE experiences ADD COLUMN unexpected TEXT")
        elif damage == "metadata":
            db.execute("DELETE FROM experience_schema")
        elif damage == "source":
            db.execute("UPDATE experience_schema SET source_layout='guessed'")
        else:
            db.execute("CREATE VIEW unexpected AS SELECT * FROM experiences")
    before = path.read_bytes()
    for call in (
        lambda: ExperienceStore(path, clock=lambda: NOW),
        lambda: ExperienceStore(path, clock=lambda: NOW, read_only=True),
        store.audit,
        lambda: store.capture(record(), ACTOR),
    ):
        with pytest.raises(ExperienceSchemaError, match="experience_schema_invalid"):
            call()
        assert path.read_bytes() == before


@pytest.mark.parametrize("damage", ["origin", "receipt", "review", "orphan", "scope", "body"])
def test_failed_migration_never_publishes_or_repairs_evidence(sources, damage):
    registry, feedback, store, item = sources
    dataset_id = uuid4()
    registry.freeze(dataset_id, (feedback.feedback_id,), split="training")
    capture(sources, dataset_id)
    with sqlite3.connect(store.path) as db:
        if damage == "origin":
            db.execute("DELETE FROM experience_origins")
        elif damage == "receipt":
            db.execute("DELETE FROM experience_feedback_sources")
        elif damage == "review":
            db.execute("UPDATE experiences SET state='active'")
        elif damage == "orphan":
            db.execute("INSERT INTO experience_reviews VALUES('orphan','reviewer',1)")
        elif damage == "scope":
            db.execute("UPDATE experiences SET scope='broken'")
        else:
            db.execute("UPDATE experiences SET body='{}'")
    legacy(store.path, "complete-v0")
    before = store.path.read_bytes()
    output = store.path.with_name("refused.sqlite")
    with pytest.raises(ExperienceSchemaError, match="experience_migration_integrity_failure"):
        migrate_legacy_store(store.path, output, legacy_layout="complete-v0")
    assert store.path.read_bytes() == before
    assert not output.exists()
    assert not list(output.parent.glob(".experience-migration-*"))


def test_wrong_layout_and_existing_output_never_overwrite(tmp_path):
    path, output = tmp_path / "old.sqlite", tmp_path / "output.sqlite"
    ExperienceStore(path, clock=lambda: NOW)
    legacy(path, "complete-v0")
    before = path.read_bytes()
    with pytest.raises(ExperienceSchemaError, match="experience_schema_invalid"):
        migrate_legacy_store(path, output, legacy_layout="manual-v0")
    assert not output.exists()
    output.write_bytes(b"preserve this output")
    with pytest.raises(FileExistsError):
        migrate_legacy_store(path, output, legacy_layout="complete-v0")
    assert output.read_bytes() == b"preserve this output"
    assert path.read_bytes() == before


def test_cli_migrates_into_a_new_readable_store(tmp_path):
    path, output = tmp_path / "old.sqlite", tmp_path / "output.sqlite"
    ExperienceStore(path, clock=lambda: NOW)
    legacy(path, "manual-v0")
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "generaldataagent.experience.migrations",
            "--source",
            str(path),
            "--output",
            str(output),
            "--legacy-layout",
            "manual-v0",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(run.stdout)["schema_version"] == 1
    assert ExperienceStore(output, clock=lambda: NOW, read_only=True).audit()["consistent"]


def test_parallel_initializers_do_not_reinitialize_existing_records(tmp_path):
    path = tmp_path / "shared.sqlite"
    with ThreadPoolExecutor(max_workers=4) as pool:
        stores = list(pool.map(lambda _: ExperienceStore(path, clock=lambda: NOW), range(8)))
    stores[0].capture(record(), ACTOR)
    assert all(store.audit()["records"] == 1 for store in stores)


def test_atomic_publication_refuses_a_concurrent_output(tmp_path, monkeypatch):
    import generaldataagent.experience.migrations as module

    path, output = tmp_path / "old.sqlite", tmp_path / "raced.sqlite"
    ExperienceStore(path, clock=lambda: NOW)
    legacy(path, "complete-v0")
    link = module.os.link

    def race(source, destination):
        Path(destination).write_bytes(b"other publisher")
        link(source, destination)

    monkeypatch.setattr(module.os, "link", race)
    with pytest.raises(FileExistsError):
        migrate_legacy_store(path, output, legacy_layout="complete-v0")
    assert output.read_bytes() == b"other publisher"
    assert not list(tmp_path.glob(".experience-migration-*"))


def test_copy_includes_committed_wal_records_without_checkpointing_source(tmp_path):
    path, output = tmp_path / "wal.sqlite", tmp_path / "migrated.sqlite"
    store = ExperienceStore(path, clock=lambda: NOW)
    legacy(path, "complete-v0")
    with sqlite3.connect(path) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        item = record()
        writer.execute(
            "INSERT INTO experiences VALUES(?,?,?,?,'candidate')",
            (
                str(item.experience_id),
                store._scope(ACTOR),
                item.model_dump_json(),
                content_hash(item.model_dump(mode="json")),
            ),
        )
        writer.execute(
            "INSERT INTO experience_origins VALUES(?,'manual')", (str(item.experience_id),)
        )
        writer.commit()
        before, wal = path.read_bytes(), Path(str(path) + "-wal").read_bytes()
        report = migrate_legacy_store(path, output, legacy_layout="complete-v0")
        assert report["records"] == 1
        assert path.read_bytes() == before
        assert Path(str(path) + "-wal").read_bytes() == wal
    assert (
        ExperienceStore(output, clock=lambda: NOW, read_only=True).state(item.experience_id, ACTOR)
        == "candidate"
    )


@pytest.mark.parametrize("problem", ["unknown_layout", "wrong_columns", "trigger", "dual_receipts"])
def test_legacy_inputs_must_match_supported_layout_and_integrity(tmp_path, sources, problem):
    registry, feedback, store, item = sources
    dataset_id = uuid4()
    registry.freeze(dataset_id, (feedback.feedback_id,), split="training")
    capture(sources, dataset_id)
    legacy(store.path, "complete-v0")
    with sqlite3.connect(store.path) as db:
        if problem == "wrong_columns":
            db.execute("ALTER TABLE experiences ADD COLUMN surprise TEXT")
        elif problem == "trigger":
            db.execute(
                "CREATE TRIGGER change_record AFTER INSERT ON experience_origins "
                "BEGIN UPDATE experiences SET state='active'; END"
            )
        elif problem == "dual_receipts":
            db.execute(
                "INSERT INTO experience_extraction_sources VALUES(?, '{}', 'broken')",
                (str(item.experience_id),),
            )
    before = store.path.read_bytes()
    output = tmp_path / "invalid-output.sqlite"
    code = {
        "unknown_layout": "experience_legacy_layout_unknown",
        "wrong_columns": "experience_schema_invalid",
        "trigger": "experience_schema_invalid",
        "dual_receipts": "experience_migration_integrity_failure",
    }[problem]
    with pytest.raises(ExperienceSchemaError, match=code):
        migrate_legacy_store(
            store.path,
            output,
            legacy_layout=("guess" if problem == "unknown_layout" else "complete-v0"),
        )
    assert store.path.read_bytes() == before
    assert not output.exists()
