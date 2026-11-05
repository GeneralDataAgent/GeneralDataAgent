import base64
import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from test_metric_analysis import actor
from test_stage11_task_submission import credentials
from test_stage13_csv_intake import RAW, build_service, confirmation

from generaldataagent.artifacts.governed import GovernedArtifactStore
from generaldataagent.datasets.csv_maintenance import (
    CsvMaintenanceInventory,
    CsvMaintenanceUnavailable,
    load_key_file,
    main,
)
from generaldataagent.datasets.csv_maintenance_contracts import CsvMaintenancePlan

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
KEY = b"i" * 32


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


def inventory_for(tmp_path, clock):
    return CsvMaintenanceInventory(
        store_path=tmp_path / "csv.db",
        encryption_key=KEY,
        artifact_index=tmp_path / "artifact-index.db",
        clock=clock,
    )


def stage(service, token, key="file"):
    return service.stage(RAW, actor(), token, key)


def profile(service, token, actor_context=None):
    owner = actor_context or actor()
    preview = service.stage(RAW, owner, token, f"profile-{uuid4()}")
    result = service.confirm(
        preview.file.file_id,
        owner,
        token,
        confirmation(preview),
    )
    return preview, result


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_read_only_inventory_reports_expired_capacity_without_values_or_paths(tmp_path):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60)
    token = credentials_token(service)
    staged = stage(service, token, "staged")
    _, result = profile(service, token)
    before = {
        name: digest(path)
        for name, path in (
            ("store", tmp_path / "csv.db"),
            ("index", tmp_path / "artifact-index.db"),
            ("budgets", tmp_path / "budgets.db"),
            ("audit", tmp_path / "audit.jsonl"),
        )
    }
    content_before = sorted(str(p.relative_to(tmp_path)) for p in (tmp_path / "content").rglob("*"))
    files_before = sorted(p.name for p in tmp_path.iterdir())
    clock.now = NOW + timedelta(seconds=61)

    plan = inventory_for(tmp_path, clock).inventory()

    assert plan.status == "blocked"
    assert plan.summary.retained_files == 2
    assert plan.summary.expired_files == 2
    assert plan.summary.review_required_files == 1
    assert plan.summary.blocked_files == 1
    assert plan.summary.staged_files == 1 and plan.summary.profiled_files == 1
    assert plan.summary.expired_source_bytes == len(RAW) * 2
    assert plan.destructive_actions_performed is False
    by_id = {str(item.file_id): item for item in plan.files}
    assert by_id[str(staged.file.file_id)].review_state == "expired_review_required"
    profiled = by_id[str(result.file.file_id)]
    assert profiled.report_state == "readable"
    assert profiled.report_digest and profiled.report_digest.startswith("sha256:")
    assert profiled.evidence_artifact_ids == tuple(
        sorted(ref.artifact_id for ref in result.evidence)
    )
    assert profiled.blocked_reasons == ("evidence_expired",)
    assert plan.inventory_digest == inventory_for(tmp_path, clock).inventory().inventory_digest

    serialized = json.dumps(plan.model_dump(mode="json"))
    assert '"001"' not in serialized and "=SUM" not in serialized
    assert "amount" not in serialized and "sample_rows" not in serialized
    assert str(tmp_path) not in serialized and "uploads" not in serialized
    assert "user-1" not in serialized and "tenant-1" not in serialized
    assert "project-1" not in serialized
    assert before["store"] == digest(tmp_path / "csv.db")
    assert before["index"] == digest(tmp_path / "artifact-index.db")
    assert before["budgets"] == digest(tmp_path / "budgets.db")
    assert before["audit"] == digest(tmp_path / "audit.jsonl")
    assert content_before == sorted(
        str(p.relative_to(tmp_path)) for p in (tmp_path / "content").rglob("*")
    )
    assert files_before == sorted(p.name for p in tmp_path.iterdir())


def test_legal_hold_marks_the_expired_profile_blocked(tmp_path):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60)
    token = credentials_token(service)
    held_preview, held_result = profile(service, token)
    other_preview, _ = profile(service, token)
    GovernedArtifactStore(
        tmp_path / "content",
        index_path=tmp_path / "artifact-index.db",
        actor=actor(),
        retention_seconds=60,
        clock=clock,
    ).set_legal_hold(held_result.evidence[0], held=True)
    clock.now = NOW + timedelta(seconds=61)

    plan = inventory_for(tmp_path, clock).inventory()

    by_id = {str(item.file_id): item for item in plan.files}
    assert "legal_hold" in by_id[str(held_preview.file.file_id)].blocked_reasons
    assert by_id[str(held_preview.file.file_id)].review_state == "blocked"
    other = by_id[str(other_preview.file.file_id)]
    assert other.blocked_reasons == ("evidence_expired",)
    assert plan.summary.blocked_files == 2


def test_unreadable_report_blocks_plan_without_partial_cleanup_permission(tmp_path):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60)
    token = credentials_token(service)
    preview, _ = profile(service, token)
    stage(service, token, "staged")
    with sqlite3.connect(tmp_path / "csv.db") as db:
        db.execute(
            "UPDATE csv_uploads SET report=? WHERE file_id=?",
            (b"not-an-encrypted-report", str(preview.file.file_id)),
        )
    clock.now = NOW + timedelta(seconds=61)

    plan = inventory_for(tmp_path, clock).inventory()

    broken = next(item for item in plan.files if item.file_id == preview.file.file_id)
    assert broken.report_state == "unreadable"
    assert broken.report_digest is None and broken.evidence_artifact_ids == ()
    assert broken.blocked_reasons == ("report_unreadable",)
    assert plan.status == "blocked"
    assert plan.summary.review_required_files == 1


def test_missing_evidence_reference_blocks_the_profiled_file(tmp_path):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60)
    token = credentials_token(service)
    preview, result = profile(service, token)
    with sqlite3.connect(tmp_path / "artifact-index.db") as db:
        db.execute(
            "DELETE FROM governed_artifacts_v2 WHERE artifact_id=?",
            (str(result.evidence[0].artifact_id),),
        )
    clock.now = NOW + timedelta(seconds=61)

    plan = inventory_for(tmp_path, clock).inventory()

    item = next(entry for entry in plan.files if entry.file_id == preview.file.file_id)
    assert "evidence_missing" in item.blocked_reasons
    assert item.review_state == "blocked"


def test_scope_mismatched_evidence_blocks_the_profiled_file(tmp_path):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60)
    token = credentials_token(service)
    preview, result = profile(service, token)
    with sqlite3.connect(tmp_path / "artifact-index.db") as db:
        db.execute(
            "UPDATE governed_artifacts_v2 SET tenant_id='other' WHERE artifact_id=?",
            (str(result.evidence[0].artifact_id),),
        )
    clock.now = NOW + timedelta(seconds=61)

    item = next(
        entry
        for entry in inventory_for(tmp_path, clock).inventory().files
        if entry.file_id == preview.file.file_id
    )
    assert "evidence_scope_mismatch" in item.blocked_reasons


def test_deleted_evidence_blocks_the_profiled_file(tmp_path):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60)
    token = credentials_token(service)
    preview, result = profile(service, token)
    with sqlite3.connect(tmp_path / "artifact-index.db") as db:
        db.execute(
            "UPDATE governed_artifacts_v2 SET deleted=1 WHERE artifact_id=?",
            (str(result.evidence[0].artifact_id),),
        )
    clock.now = NOW + timedelta(seconds=61)

    item = next(
        entry
        for entry in inventory_for(tmp_path, clock).inventory().files
        if entry.file_id == preview.file.file_id
    )
    assert "evidence_deleted" in item.blocked_reasons


def test_artifact_index_layout_mismatch_rejects_the_observation(tmp_path):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60)
    token = credentials_token(service)
    profile(service, token)
    with sqlite3.connect(tmp_path / "artifact-index.db") as db:
        db.execute("ALTER TABLE governed_artifacts_v2 RENAME COLUMN held TO held_at")
    clock.now = NOW + timedelta(seconds=61)

    with pytest.raises(CsvMaintenanceUnavailable, match="artifact_index_unavailable"):
        inventory_for(tmp_path, clock).inventory()


def test_missing_artifact_index_rejects_expired_profiled_inventory(tmp_path):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60)
    token = credentials_token(service)
    profile(service, token)
    (tmp_path / "artifact-index.db").unlink()
    clock.now = NOW + timedelta(seconds=61)

    with pytest.raises(CsvMaintenanceUnavailable, match="artifact_index_unavailable"):
        inventory_for(tmp_path, clock).inventory()


@pytest.mark.parametrize("mutation", ["wrong_key", "missing_store", "corrupt_record", "schema"])
def test_store_key_and_record_failures_never_emit_a_partial_plan(tmp_path, mutation):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60)
    token = credentials_token(service)
    preview = stage(service, token)
    if mutation == "wrong_key":
        port = CsvMaintenanceInventory(
            store_path=tmp_path / "csv.db",
            encryption_key=b"w" * 32,
            artifact_index=tmp_path / "artifact-index.db",
            clock=clock,
        )
        with pytest.raises(PermissionError, match="key_mismatch"):
            port.inventory()
        return
    if mutation == "missing_store":
        path = tmp_path / "absent.db"
        port = CsvMaintenanceInventory(
            store_path=path,
            encryption_key=KEY,
            artifact_index=tmp_path / "artifact-index.db",
            clock=clock,
        )
        with pytest.raises(CsvMaintenanceUnavailable):
            port.inventory()
        assert not path.exists()
        return
    with sqlite3.connect(tmp_path / "csv.db") as db:
        if mutation == "corrupt_record":
            db.execute(
                "UPDATE csv_uploads SET body=? WHERE file_id=?",
                (b"corrupt", str(preview.file.file_id)),
            )
        else:
            db.execute("PRAGMA user_version=2")
    with pytest.raises(CsvMaintenanceUnavailable):
        inventory_for(tmp_path, clock).inventory()


def test_digest_tracks_store_changes_not_wall_clock(tmp_path):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60)
    token = credentials_token(service)
    first = stage(service, token, "first")
    second = stage(service, token, "second")

    initial = inventory_for(tmp_path, clock).inventory()
    clock.now = NOW + timedelta(seconds=1)
    later = inventory_for(tmp_path, clock).inventory()
    assert initial.observed_at != later.observed_at
    assert initial.inventory_digest == later.inventory_digest
    assert {item.review_state for item in later.files} == {"retain"}

    service.confirm(first.file.file_id, actor(), token, confirmation(first))
    changed = inventory_for(tmp_path, clock).inventory()
    assert changed.inventory_digest != initial.inventory_digest
    assert changed.status == "complete"
    assert {item.file_id for item in changed.files} == {
        first.file.file_id,
        second.file.file_id,
    }
    assert any(item.report_state == "readable" for item in changed.files)


def test_release_drift_is_visible_without_reinterpreting_the_source(tmp_path):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60)
    token = credentials_token(service)
    profile(service, token)
    clock.now = NOW + timedelta(seconds=61)
    current = inventory_for(tmp_path, clock).inventory()
    drifted = CsvMaintenanceInventory(
        store_path=tmp_path / "csv.db",
        encryption_key=KEY,
        artifact_index=tmp_path / "artifact-index.db",
        clock=clock,
        parser_fingerprint="sha256:" + "0" * 64,
    ).inventory()
    assert drifted.epoch != current.epoch
    assert drifted.inventory_digest != current.inventory_digest
    assert {item.availability for item in drifted.files} == {"release_incompatible"}
    assert drifted.files[0].source_hash == current.files[0].source_hash


def test_cli_reports_complete_blocked_and_private_key_failures(tmp_path, capsys):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60)
    token = credentials_token(service)
    stage(service, token, "staged")
    clock.now = NOW + timedelta(seconds=61)
    key_file = tmp_path / "maintenance.key"
    key_file.write_bytes(base64.b64encode(KEY) + b"\n")
    key_file.chmod(0o600)
    common = [
        "--store",
        str(tmp_path / "csv.db"),
        "--key-file",
        str(key_file),
        "--artifact-index",
        str(tmp_path / "artifact-index.db"),
        "--summary",
    ]
    assert main(common) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "complete"
    assert output["summary"]["review_required_files"] == 1

    key_file.chmod(0o644)
    assert main(common) == 2
    assert "not_private" in capsys.readouterr().err
    key_file.chmod(0o600)
    assert load_key_file(key_file) == KEY


def test_cli_exit_three_when_review_is_blocked(tmp_path, capsys):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60)
    token = credentials_token(service)
    profile(service, token)
    clock.now = NOW + timedelta(seconds=61)
    key_file = tmp_path / "maintenance.key"
    key_file.write_bytes(base64.b64encode(KEY))
    key_file.chmod(0o600)
    assert (
        main(
            [
                "--store",
                str(tmp_path / "csv.db"),
                "--key-file",
                str(key_file),
                "--artifact-index",
                str(tmp_path / "artifact-index.db"),
            ]
        )
        == 3
    )
    plan = json.loads(capsys.readouterr().out)
    assert plan["status"] == "blocked"
    assert plan["summary"]["blocked_files"] == 1
    assert plan["destructive_actions_performed"] is False


def test_key_file_rejects_symlinks_and_malformed_content(tmp_path):
    real = tmp_path / "real.key"
    real.write_bytes(base64.b64encode(KEY))
    real.chmod(0o600)
    link = tmp_path / "link.key"
    link.symlink_to(real)
    with pytest.raises(CsvMaintenanceUnavailable, match="key_file_invalid"):
        load_key_file(link)
    short = tmp_path / "short.key"
    short.write_bytes(b"c2hvcnQ=")
    short.chmod(0o600)
    with pytest.raises(CsvMaintenanceUnavailable, match="key_file_invalid"):
        load_key_file(short)


def test_plan_rejects_a_tampered_digest_or_expiry_observation(tmp_path):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60)
    token = credentials_token(service)
    stage(service, token)
    clock.now = NOW + timedelta(seconds=61)
    plan = inventory_for(tmp_path, clock).inventory()
    for field, value in (
        ("inventory_digest", "sha256:" + "0" * 64),
        (
            "files",
            (plan.files[0].model_copy(update={"expired": False}),),
        ),
    ):
        with pytest.raises(ValueError):
            CsvMaintenancePlan(**(plan.model_dump() | {field: value}))


def test_cli_exit_four_when_state_cannot_be_observed(tmp_path, capsys):
    key_file = tmp_path / "maintenance.key"
    key_file.write_bytes(base64.b64encode(KEY))
    key_file.chmod(0o600)
    assert (
        main(
            [
                "--store",
                str(tmp_path / "absent.db"),
                "--key-file",
                str(key_file),
                "--artifact-index",
                str(tmp_path / "artifact-index.db"),
            ]
        )
        == 4
    )
    assert "store_unavailable" in capsys.readouterr().err


def credentials_token(service):
    token, _ = credentials(service.broker)
    return token
