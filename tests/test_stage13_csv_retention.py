import hashlib
import json
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from test_metric_analysis import actor
from test_stage11_task_submission import credentials
from test_stage13_csv_intake import RAW, build_service, confirmation

from generaldataagent.datasets.csv_intake_store import CsvIntakeUnavailable, CsvUploadStore
from generaldataagent.datasets.csv_retention import (
    CsvRetentionConflict,
    CsvRetentionInspector,
    CsvRetentionPlan,
)
from generaldataagent.services.csv_retention import main


def setup(tmp_path, *, retention=10):
    now = [datetime(2026, 11, 4, tzinfo=UTC)]
    service = build_service(tmp_path, clock=lambda: now[0], retention=retention)
    token, _ = credentials(service.broker)
    reader = CsvUploadStore(
        service.store.path, encryption_key=b"i" * 32, clock=lambda: now[0], read_only=True
    )
    return service, CsvRetentionInspector(reader, service.broker, audit=service.audit), token, now


def snapshot(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_real_profile_and_staged_expiry_inventory_no_source_or_budget_writes(tmp_path):
    service, reader, token, now = setup(tmp_path)
    first = service.stage(RAW, actor(), token, "one")
    result = service.confirm(first.file.file_id, actor(), token, confirmation(first))
    second = service.stage(b"key\nprivate-secret\n", actor(), token, "two")
    now[0] += timedelta(seconds=11)
    before = {
        p: snapshot(p) for p in [service.store.path, service.budgets.path, service.artifact_index]
    }
    plan = reader.plan(actor(), token)
    assert plan.retained_files == plan.expired_files == 2
    assert plan.retained_raw_bytes == len(RAW) + len(b"key\nprivate-secret\n")
    entries = {e.file_id: e for e in plan.entries}
    assert set(entries[first.file.file_id].report_artifact_ids) == {
        r.artifact_id for r in result.evidence
    }
    assert entries[first.file.file_id].report_state == "registered_not_verified"
    assert entries[second.file.file_id].report_state == "none_registered"
    assert all(
        e.hold_state == "unknown" and e.decision == "blocked_hold_unknown" for e in plan.entries
    )
    text = plan.model_dump_json()
    for secret in [
        "private-secret",
        "=SUM",
        token,
        str(service.store.path),
        "sample_rows",
        "source_base64",
        "amount",
        actor().user_id,
    ]:
        assert secret not in text
    assert not plan.deletion_authorized and plan.quota_released == 0
    assert (
        reader.verify(actor(), token, CsvRetentionPlan.model_validate_json(text)).retained_files
        == 2
    )
    assert {p: snapshot(p) for p in before} == before
    with pytest.raises(PermissionError):
        service.stage(RAW, actor(), token, "one")
    assert snapshot(service.store.path) == before[service.store.path]


def test_empty_active_and_exact_expiry_boundary(tmp_path):
    service, reader, token, now = setup(tmp_path)
    empty = reader.plan(actor(), token)
    assert empty.retained_files == empty.expired_files == 0
    assert not reader.verify(actor(), token, empty).deletion_authorized
    item = service.stage(RAW, actor(), token, "a")
    plan = reader.plan(actor(), token)
    assert plan.expired_files == 0 and plan.entries[0].decision == "retain_active"
    now[0] = item.file.expires_at
    with pytest.raises(CsvRetentionConflict):
        reader.verify(actor(), token, plan)
    assert reader.plan(actor(), token).expired_files == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("user_id", "other"),
        ("tenant_id", "other"),
        ("project_id", "other"),
        ("data_domains", ("finance",)),
        ("data_domains", ("commerce", "finance")),
    ],
)
def test_scope_and_grant_are_current_not_supplied_actor_authority(tmp_path, field, value):
    service, reader, token, _ = setup(tmp_path)
    service.stage(RAW, actor(), token, "a")
    other = actor().model_copy(update={field: value})
    with pytest.raises(PermissionError):
        reader.plan(other, token)
    other_token, _ = credentials(service.broker, other)
    assert reader.plan(other, other_token).retained_files == 0
    with pytest.raises(CsvRetentionConflict):
        reader.verify(other, other_token, reader.plan(actor(), token))


def test_readonly_missing_empty_wrong_key_symlink_and_direct_write_refused(tmp_path):
    missing = tmp_path / "not-created" / "source.db"
    with pytest.raises(PermissionError):
        CsvUploadStore(missing, encryption_key=b"i" * 32, read_only=True)
    assert not missing.parent.exists()
    empty = tmp_path / "empty.db"
    empty.touch()
    with pytest.raises(ValueError):
        CsvUploadStore(empty, encryption_key=b"i" * 32, read_only=True)
    assert empty.stat().st_size == 0
    service, reader, _, _ = setup(tmp_path / "real")
    with pytest.raises(PermissionError):
        CsvUploadStore(service.store.path, encryption_key=b"z" * 32, read_only=True)
    link = tmp_path / "link.db"
    link.symlink_to(service.store.path)
    with pytest.raises(PermissionError):
        CsvUploadStore(link, encryption_key=b"i" * 32, read_only=True)
    with pytest.raises(PermissionError), reader.store.db(write=True):
        pass
    with pytest.raises(CsvIntakeUnavailable), reader.store.db() as db:
        db.execute("DELETE FROM csv_uploads")
    with pytest.raises(ValueError):
        CsvRetentionInspector(service.store, service.broker, audit=service.audit)
    with pytest.raises(ValueError):
        CsvUploadStore(service.store.path, encryption_key=b"i" * 32, read_only=1)


@pytest.mark.parametrize(
    "kind",
    ["new_source", "registered_report", "expired", "clock_backwards", "plan_expired", "clone_path"],
)
def test_reviewed_plan_detects_committed_or_temporal_drift(tmp_path, kind):
    service, reader, token, now = setup(tmp_path)
    item = service.stage(RAW, actor(), token, "a")
    plan = reader.plan(actor(), token)
    if kind == "new_source":
        service.stage(b"key\nvalue\n", actor(), token, "b")
    if kind == "registered_report":
        service.confirm(item.file.file_id, actor(), token, confirmation(item))
    if kind == "expired":
        now[0] += timedelta(seconds=11)
    if kind == "clock_backwards":
        now[0] -= timedelta(seconds=1)
    if kind == "plan_expired":
        now[0] += timedelta(seconds=300)
    if kind == "clone_path":
        clone = tmp_path / "clone.db"
        shutil.copy2(service.store.path, clone)
        reader = CsvRetentionInspector(
            CsvUploadStore(clone, encryption_key=b"i" * 32, read_only=True, clock=lambda: now[0]),
            service.broker,
            audit=service.audit,
        )
    with pytest.raises(CsvRetentionConflict):
        reader.verify(actor(), token, plan)


@pytest.mark.parametrize("change", ["token", "digest", "omit", "decision", "approval"])
def test_plan_tampering_cannot_turn_comparison_into_cleanup_authority(tmp_path, change):
    service, reader, token, _ = setup(tmp_path)
    service.stage(RAW, actor(), token, "a")
    plan = reader.plan(actor(), token)
    value = plan.model_dump(mode="json")
    if change == "token":
        value["plan_token"] = "0" * 64
    if change == "digest":
        value["snapshot_digest"] = "sha256:" + "0" * 64
    if change == "omit":
        value.update(entries=[], retained_files=0, retained_raw_bytes=0)
    if change == "decision":
        value["entries"][0]["decision"] = "delete"
    if change == "approval":
        value["deletion_authorized"] = True
    with pytest.raises((ValueError, CsvRetentionConflict)):
        reader.verify(actor(), token, CsvRetentionPlan.model_validate(value))


@pytest.mark.parametrize("part", ["body", "report", "version", "layout", "key"])
def test_live_store_corruption_denies_without_partial_inventory_and_recovers(tmp_path, part):
    service, reader, token, _ = setup(tmp_path)
    item = service.stage(RAW, actor(), token, "a")
    backup = service.store.path.read_bytes()
    with sqlite3.connect(service.store.path) as db:
        if part == "body":
            db.execute("UPDATE csv_uploads SET body=?", (b"bad",))
        if part == "report":
            db.execute("UPDATE csv_uploads SET report=?", (b"bad",))
        if part == "version":
            db.execute("PRAGMA user_version=99")
        if part == "layout":
            db.execute("ALTER TABLE csv_uploads ADD COLUMN other TEXT")
        if part == "key":
            db.execute("UPDATE csv_key SET body=?", (b"bad",))
    with pytest.raises((PermissionError, CsvIntakeUnavailable)):
        reader.plan(actor(), token)
    service.store.path.write_bytes(backup)
    assert reader.plan(actor(), token).entries[0].file_id == item.file.file_id


def test_invalid_but_encrypted_cross_file_report_is_not_treated_as_valid_reference(tmp_path):
    service, reader, token, _ = setup(tmp_path)
    item = service.stage(RAW, actor(), token, "a")
    result = service.confirm(item.file.file_id, actor(), token, confirmation(item))
    forged = result.model_dump(mode="json")
    forged["file"]["file_id"] = str(uuid4())
    with service.store.db(write=True) as db:
        row = db.execute("SELECT * FROM csv_uploads").fetchone()
        db.execute(
            "UPDATE csv_uploads SET report=?",
            (
                service.store.seal(
                    json.dumps(forged).encode(), b"csv-report-v1:" + service.store.aad(row)
                ),
            ),
        )
    with pytest.raises(CsvIntakeUnavailable):
        reader.plan(actor(), token)


def test_live_authorization_revoked_during_scan_does_not_leak_partial_metadata(tmp_path):
    service, reader, token, _ = setup(tmp_path)
    service.stage(RAW, actor(), token, "a")
    original = reader.store.decode

    def revoke(row):
        result = original(row)
        service.broker.revoke(token)
        return result

    reader.store.decode = revoke
    with pytest.raises(PermissionError):
        reader.plan(actor(), token)
    fresh, _ = credentials(service.broker)
    reader.store.decode = original
    assert reader.plan(actor(), fresh).retained_files == 1
    forbidden, _ = credentials(service.broker, permissions=frozenset())
    with pytest.raises(PermissionError):
        reader.plan(actor(), forbidden)


def test_snapshot_transaction_does_not_splice_concurrent_new_row_and_verify_detects_it(tmp_path):
    service, reader, token, _ = setup(tmp_path)
    service.stage(RAW, actor(), token, "a")
    with sqlite3.connect(service.store.path) as db:
        db.execute("PRAGMA journal_mode=WAL")
    original = reader.store.decode

    def concurrent(row):
        value = original(row)
        service.stage(b"key\nnew\n", actor(), token, "b")
        return value

    reader.store.decode = concurrent
    plan = reader.plan(actor(), token)
    assert plan.retained_files == 1
    reader.store.decode = original
    with pytest.raises(CsvRetentionConflict):
        reader.verify(actor(), token, plan)
    assert reader.plan(actor(), token).retained_files == 2


def test_monotonic_budget_fails_closed(tmp_path):
    service, reader, token, _ = setup(tmp_path)
    service.stage(RAW, actor(), token, "a")
    ticks = iter([0, 0, 6])
    reader.clock = lambda: next(ticks)
    with pytest.raises(PermissionError):
        reader.plan(actor(), token)


def test_cli_default_does_not_load_factory_and_failures_are_fixed(tmp_path, monkeypatch, capsys):
    assert main(["--factory", "private_missing:factory", "--data-domain", "commerce"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "explicit_local_inventory_mode_required"
    assert (
        main(["--development", "--factory", "private_missing:factory", "--data-domain", "commerce"])
        == 2
    )
    assert capsys.readouterr().out == '{"status": "csv_retention_unavailable"}\n'
    assert not list(tmp_path.iterdir())


def test_retained_cap_still_counts_expired_rows_and_no_partial_plan_over_bound(tmp_path):
    service, reader, token, now = setup(tmp_path)
    for n in range(20):
        service.stage(RAW, actor(), token, f"source-{n}")
    now[0] += timedelta(seconds=11)
    plan = reader.plan(actor(), token)
    assert plan.expired_files == plan.retained_files == 20 and plan.quota_released == 0
    from generaldataagent.datasets.csv_intake_store import CsvIntakeConflict

    with pytest.raises(CsvIntakeConflict):
        service.stage(RAW, actor(), token, "over-limit")
    # Inject inconsistent over-capacity storage; never truncate to a complete plan.
    service.store.max_files_per_actor = 21
    service.stage(RAW, actor(), token, "inconsistent-capacity")
    with pytest.raises(CsvIntakeUnavailable):
        reader.plan(actor(), token)


def test_live_readonly_path_loss_replacement_and_naive_clock_refused(tmp_path):
    service, reader, token, now = setup(tmp_path)
    service.stage(RAW, actor(), token, "a")
    old = service.store.path.read_bytes()
    service.store.path.unlink()
    with pytest.raises(PermissionError):
        reader.plan(actor(), token)
    assert not service.store.path.exists()
    alternate = CsvUploadStore(tmp_path / "alternate.db", encryption_key=b"z" * 32)
    shutil.copy2(alternate.path, service.store.path)
    with pytest.raises(PermissionError):
        reader.plan(actor(), token)
    service.store.path.write_bytes(old)
    reader.store.clock = lambda: datetime(2026, 11, 4)
    with pytest.raises(ValueError):
        reader.plan(actor(), token)


def test_oversized_sqlite_row_refuses_before_json_delivery(tmp_path):
    service, reader, token, _ = setup(tmp_path)
    service.stage(RAW, actor(), token, "a")
    with sqlite3.connect(service.store.path) as db:
        db.execute("UPDATE csv_uploads SET body=zeroblob(16777217)")
    with pytest.raises(CsvIntakeUnavailable):
        reader.plan(actor(), token)


def test_cli_real_reader_plan_compare_conflict_auth_and_bounded_file(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    from generaldataagent.services import csv_retention

    service, reader, token, _ = setup(tmp_path)
    service.stage(RAW, actor(), token, "a")
    monkeypatch.setenv("GDA_CSV_RETENTION_TOKEN", token)
    monkeypatch.setattr(
        csv_retention.importlib, "import_module", lambda _: SimpleNamespace(reader=lambda: reader)
    )
    args = ["--development", "--factory", "trusted:reader", "--data-domain", "commerce"]
    assert main(args) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["retained_files"] == 1
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(value))
    assert main(args + ["--verify-plan", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["deletion_authorized"] is False
    service.stage(b"key\nnext\n", actor(), token, "b")
    assert main(args + ["--verify-plan", str(path)]) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "csv_retention_plan_conflict"
    path.write_bytes(b"x" * 65537)
    assert main(args + ["--verify-plan", str(path)]) == 2
    assert capsys.readouterr().out == '{"status": "csv_retention_unavailable"}\n'
    monkeypatch.setenv("GDA_CSV_RETENTION_TOKEN", "never-print-this-token")
    assert main(args) == 2
    assert capsys.readouterr().out == '{"status": "csv_retention_unavailable"}\n'
    monkeypatch.setattr(
        csv_retention.importlib, "import_module", lambda _: SimpleNamespace(reader=lambda: object())
    )
    assert main(args) == 2
    assert capsys.readouterr().out == '{"status": "csv_retention_unavailable"}\n'


def test_cli_rejects_factory_expression_and_no_apply_option(capsys):
    assert (
        main(["--development", "--factory", "trusted:reader()", "--data-domain", "commerce"]) == 2
    )
    assert capsys.readouterr().out == '{"status": "csv_retention_unavailable"}\n'
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "--development",
                "--factory",
                "trusted:reader",
                "--data-domain",
                "commerce",
                "--apply-plan",
                "anything",
            ]
        )
    assert exc.value.code == 2


def test_audit_failure_blocks_metadata_and_revocation_during_emit_still_denies(tmp_path):
    from types import SimpleNamespace

    service, reader, token, _ = setup(tmp_path)
    service.stage(RAW, actor(), token, "a")
    plan = reader.plan(actor(), token)

    def failed(_):
        raise RuntimeError("private audit path")

    reader.audit = SimpleNamespace(emit=failed)
    with pytest.raises(CsvIntakeUnavailable):
        reader.plan(actor(), token)
    with pytest.raises(CsvIntakeUnavailable):
        reader.verify(actor(), token, plan)
    reader.audit = SimpleNamespace(emit=lambda _: service.broker.revoke(token))
    with pytest.raises(PermissionError):
        reader.plan(actor(), token)
    with pytest.raises(ValueError):
        CsvRetentionInspector(reader.store, service.broker, audit=None)


def test_missing_report_content_is_registered_not_verified_and_never_deletion_clearance(tmp_path):
    service, reader, token, now = setup(tmp_path)
    item = service.stage(RAW, actor(), token, "a")
    result = service.confirm(item.file.file_id, actor(), token, confirmation(item))
    ref = result.evidence[0]
    (service.artifact_root / str(ref.task_id) / f"{ref.artifact_id}.json").unlink()
    now[0] += timedelta(seconds=11)
    plan = reader.plan(actor(), token)
    assert plan.entries[0].report_state == "registered_not_verified"
    assert plan.entries[0].decision == "blocked_hold_unknown" and not plan.deletion_authorized
