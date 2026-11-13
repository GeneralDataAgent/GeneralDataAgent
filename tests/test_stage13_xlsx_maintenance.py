import base64
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from test_metric_analysis import actor
from test_stage11_task_submission import credentials
from test_stage13_xlsx_conformance import prepared
from test_stage13_xlsx_intake import (
    RAW,
    XLSX,
    build_service,
    client_for,
    declaration_body,
    declare,
    upload,
)
from test_stage13_xlsx_snapshot import snapshot

from generaldataagent.datasets import xlsx_intake
from generaldataagent.datasets.xlsx_maintenance import (
    XlsxMaintenanceInventory,
    XlsxMaintenanceUnavailable,
    load_key_file,
    main,
)
from generaldataagent.datasets.xlsx_maintenance_contracts import (
    XlsxMaintenancePlan,
    maintenance_digest,
)

KEY = b"i" * 32


def key_file(tmp_path, key=KEY):
    path = tmp_path / "xlsx-key"
    path.write_bytes(base64.b64encode(key))
    path.chmod(0o600)
    return path


def inventory(tmp_path, *, clock=None, **kwargs):
    return XlsxMaintenanceInventory(
        store_path=tmp_path / "xlsx.db",
        encryption_key=KEY,
        clock=clock,
        **kwargs,
    ).inventory()


def upload_as(client, headers, key):
    return client.post(
        "/v1/analysis/files/xlsx",
        headers=headers | {"content-type": XLSX, "idempotency-key": key},
        content=RAW,
    )


def test_inventory_reports_retained_files_without_values(tmp_path):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    client = client_for(service)
    staged = upload(client, headers).json()
    other = actor().model_copy(update={"user_id": "user-2", "tenant_id": "tenant-2"})
    _, other_headers = credentials(service.broker, other)
    file_id, _ = prepared(client, other_headers)
    assert snapshot(client, other_headers, file_id).status_code == 200
    before = (tmp_path / "xlsx.db").read_bytes()
    plan = inventory(tmp_path)
    assert (tmp_path / "xlsx.db").read_bytes() == before
    assert plan.status == "complete"
    assert plan.values == "not-read-by-this-inventory"
    assert plan.deletes is False
    assert plan.releases_capacity is False
    assert plan.audit == "not-emitted"
    assert plan.summary.retained_files == 2
    assert (plan.summary.staged_files, plan.summary.confirmed_files) == (1, 1)
    assert (plan.summary.interpreted_files, plan.summary.snapshot_files) == (1, 1)
    assert plan.summary.stale_snapshots == 0
    assert plan.summary.retained_source_bytes == sum(entry.source_bytes for entry in plan.files)
    assert plan.capacity.headroom_files == 200 - 2
    assert len(plan.scopes) == 2
    assert len({entry.owner_ref for entry in plan.files}) == 2
    assert all(len(entry.owner_ref) == 32 for entry in plan.files)
    by_id = {str(entry.file_id): entry for entry in plan.files}
    first = by_id[staged["file"]["file_id"]]
    assert (first.status, first.has_interpretation, first.snapshot_state) == (
        "staged",
        False,
        "none",
    )
    second = by_id[file_id]
    assert (second.status, second.snapshot_state) == ("confirmed", "current")
    assert (second.snapshot_rows, second.snapshot_cells) == (7, 21)
    assert second.snapshot_duplicate_rows == 1
    assert second.availability == "compatible"
    assert second.review_state == "retain"
    body = json.dumps(plan.model_dump(mode="json"), ensure_ascii=False)
    for secret in ("alpha", "amount", "flag", "2.5"):
        assert secret not in body
    assert "xlsx.maintenance" not in (tmp_path / "audit.jsonl").read_text()
    assert inventory(tmp_path).inventory_digest == plan.inventory_digest


def test_inventory_reports_expiry_and_per_scope_capacity(tmp_path):
    now = [datetime.now(UTC)]
    service = build_service(tmp_path, clock=lambda: now[0], retention=60, max_files=2)
    _, headers = credentials(service.broker)
    client = client_for(service)
    assert upload_as(client, headers, "xlsx-a").status_code == 201
    assert upload_as(client, headers, "xlsx-b").status_code == 201
    before = inventory(tmp_path, clock=lambda: now[0], max_files=2, max_files_per_actor=2)
    assert before.summary.retained_files == 2
    assert (before.summary.expired_files, before.summary.expired_source_bytes) == (0, 0)
    assert before.capacity.max_files_per_actor == 2
    assert before.capacity.headroom_files == 0
    now[0] += timedelta(seconds=61)
    after = inventory(tmp_path, clock=lambda: now[0], max_files=2, max_files_per_actor=2)
    assert after.summary.expired_files == 2
    assert after.summary.expired_source_bytes == after.summary.retained_source_bytes
    assert all(
        entry.expired and entry.review_state == "expired_review_required" for entry in after.files
    )
    assert len(after.scopes) == 1
    scope = after.scopes[0]
    assert (scope.retained_files, scope.expired_files) == (2, 2)
    assert scope.owner_ref == after.files[0].owner_ref
    assert after.inventory_digest != before.inventory_digest


def test_inventory_marks_a_revised_declaration_snapshot_stale(tmp_path):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    client = client_for(service)
    file_id, declared = prepared(client, headers)
    first = snapshot(client, headers, file_id).json()
    revised = declare(
        client,
        headers,
        file_id,
        declaration_body(
            declared,
            columns=[{"index": 0, "header": "name", "type": "text"}],
            duplicate_rows="keep-all",
        ),
    )
    assert revised.status_code == 200, revised.text
    plan = inventory(tmp_path)
    entry = plan.files[0]
    assert entry.snapshot_state == "stale"
    assert entry.snapshot_hash == first["snapshot_hash"]
    assert (entry.snapshot_rows, entry.snapshot_cells) == (7, 21)
    assert plan.summary.snapshot_files == 1
    assert plan.summary.stale_snapshots == 1


def test_inventory_reports_release_drift_without_restaging(tmp_path, monkeypatch):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    client = client_for(service)
    prepared(client, headers)
    monkeypatch.setattr(xlsx_intake, "parser_fingerprint", lambda *args: "sha256:" + "1" * 64)
    plan = inventory(tmp_path)
    assert plan.files[0].availability == "release_incompatible"
    assert plan.status == "complete"
    assert (tmp_path / "xlsx.db").exists()


def test_inventory_fails_closed_on_bad_key_or_record(tmp_path):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    upload(client_for(service), headers)
    with pytest.raises(PermissionError, match="xlsx_maintenance_key_mismatch"):
        XlsxMaintenanceInventory(
            store_path=tmp_path / "xlsx.db", encryption_key=b"j" * 32
        ).inventory()
    with sqlite3.connect(tmp_path / "xlsx.db") as db:
        db.execute("UPDATE xlsx_uploads SET body=x'00'")
    with pytest.raises(XlsxMaintenanceUnavailable, match="xlsx_maintenance_record_unavailable"):
        inventory(tmp_path)
    missing = tmp_path / "absent.db"
    with pytest.raises(XlsxMaintenanceUnavailable, match="xlsx_maintenance_store_unavailable"):
        XlsxMaintenanceInventory(store_path=missing, encryption_key=KEY).inventory()
    assert not missing.exists()


def test_inventory_requires_the_released_schema(tmp_path):
    path = tmp_path / "empty.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE xlsx_key (singleton INTEGER PRIMARY KEY, body BLOB NOT NULL)")
    with pytest.raises(XlsxMaintenanceUnavailable, match="xlsx_maintenance_schema_unsupported"):
        XlsxMaintenanceInventory(store_path=path, encryption_key=KEY).inventory()
    (tmp_path / "state").mkdir()
    service = build_service(tmp_path / "state")
    _, headers = credentials(service.broker)
    upload(client_for(service), headers)
    store = tmp_path / "state" / "xlsx.db"
    with sqlite3.connect(store) as db:
        db.execute("DELETE FROM xlsx_key")
    with pytest.raises(XlsxMaintenanceUnavailable, match="xlsx_maintenance_key_missing"):
        XlsxMaintenanceInventory(store_path=store, encryption_key=KEY).inventory()


def test_inventory_cli_emits_a_plan_and_summary(tmp_path, capsys):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    upload(client_for(service), headers)
    path = key_file(tmp_path)
    assert load_key_file(path) == KEY
    common = ["--store", str(tmp_path / "xlsx.db"), "--key-file", str(path)]
    assert main(common) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["summary"]["retained_files"] == 1
    assert plan["files"][0]["status"] == "staged"
    assert plan["deletes"] is False
    assert main(common + ["--summary"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert set(summary) == {
        "status",
        "observed_at",
        "inventory_digest",
        "capacity",
        "summary",
        "values",
        "deletes",
        "releases_capacity",
    }
    wrong = key_file(tmp_path, key=b"j" * 32)
    assert main(["--store", str(tmp_path / "xlsx.db"), "--key-file", str(wrong)]) == 2
    assert "xlsx_maintenance_key_mismatch" in capsys.readouterr().err
    absent = tmp_path / "absent-key"
    assert main(["--store", str(tmp_path / "xlsx.db"), "--key-file", str(absent)]) == 4
    assert "xlsx_maintenance_key_file_invalid" in capsys.readouterr().err


def test_maintenance_contracts_reject_incoherent_plans(tmp_path):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    upload(client_for(service), headers)
    plan = inventory(tmp_path)
    dumped = plan.model_dump(mode="json")
    with pytest.raises(ValidationError, match="xlsx_maintenance_summary_mismatch"):
        XlsxMaintenancePlan.model_validate({**dumped, "files": []})
    mutated = plan.files[0].model_copy(update={"source_bytes": 1})
    with pytest.raises(ValidationError, match="xlsx_maintenance_digest_mismatch"):
        XlsxMaintenancePlan.model_validate(
            {
                **dumped,
                "inventory_digest": maintenance_digest(plan.epoch, (mutated,)),
            }
        )
    entry = plan.files[0].model_dump(mode="json")
    with pytest.raises(ValidationError, match="xlsx_maintenance_review_state_invalid"):
        type(plan.files[0]).model_validate({**entry, "expired": True})
    with pytest.raises(ValidationError, match="xlsx_maintenance_snapshot_state_invalid"):
        type(plan.files[0]).model_validate({**entry, "snapshot_state": "current"})
    assert maintenance_digest(plan.epoch, plan.files) == plan.inventory_digest
    assert XlsxMaintenancePlan.model_validate(dumped) == plan
