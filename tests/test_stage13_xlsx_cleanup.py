import base64
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from test_stage11_task_submission import credentials
from test_stage13_xlsx_intake import RAW, XLSX, build_service, client_for

from generaldataagent.contracts import RiskLevel, ToolRequest
from generaldataagent.datasets.xlsx_cleanup import (
    XlsxCleanupConflict,
    XlsxCleanupRefused,
    XlsxCleanupUnavailable,
    XlsxSourceCleanup,
    main,
)
from generaldataagent.datasets.xlsx_cleanup_contracts import (
    XlsxCleanupReceipt,
    XlsxSourceTombstone,
    tombstone_digest,
    tombstone_payload,
)
from generaldataagent.datasets.xlsx_maintenance import (
    XlsxMaintenanceInventory,
    load_key_file,
)
from generaldataagent.policy.approvals import SQLiteApprovalStore

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
KEY = b"i" * 32


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


def key_file(tmp_path, key=KEY):
    path = tmp_path / "xlsx-key"
    path.write_bytes(base64.b64encode(key))
    path.chmod(0o600)
    return path


def plan_for(tmp_path, clock):
    return XlsxMaintenanceInventory(
        store_path=tmp_path / "xlsx.db", encryption_key=KEY, clock=clock
    ).inventory()


def cleanup_for(tmp_path, clock, key=KEY):
    return XlsxSourceCleanup(
        store_path=tmp_path / "xlsx.db",
        encryption_key=key,
        approval_db=tmp_path / "approvals.db",
        clock=clock,
    )


def upload_as(client, headers, key):
    return client.post(
        "/v1/analysis/files/xlsx",
        headers=headers | {"content-type": XLSX, "idempotency-key": key},
        content=RAW,
    )


def expire(clock, seconds=61):
    clock.now += timedelta(seconds=seconds)


def approve(tmp_path, clock, plan, file_ids, *, mutate=None, approve_it=True):
    store = SQLiteApprovalStore(tmp_path / "approvals.db", clock=clock)
    arguments = {
        "inventory_digest": plan.inventory_digest,
        "file_ids": [str(value) for value in file_ids],
    }
    fields = {
        "task_id": uuid4(),
        "user_id": "operator-requester",
        "tenant_id": "tenant-1",
        "project_id": "project-1",
        "agent_role": "data_engineer",
        "tool_name": "xlsx.maintenance.remove",
        "action": "remove-expired-source",
        "resource": plan.inventory_digest,
        "arguments": arguments,
        "data_domains": ("commerce",),
        "requested_risk": RiskLevel.R2,
    }
    if mutate is not None:
        fields = mutate(fields)
    created = store.create_request(
        ToolRequest(**fields),
        impact_scope="Remove selected expired private XLSX sources",
        cost_estimate="One local transactional delete",
        classification="internal",
        rollback_plan="Tombstones retain facts; source rows are not restored automatically",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    if approve_it:
        store.approve(created.approval_id, approver_id="independent-reviewer")
    return store, created.approval_id


def count_rows(path):
    with sqlite3.connect(path) as db:
        return db.execute("SELECT COUNT(*) FROM xlsx_uploads").fetchone()[0]


def test_approved_cleanup_removes_only_the_selected_expired_source(tmp_path):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60)
    _, headers = credentials(service.broker)
    client = client_for(service)
    first = upload_as(client, headers, "xlsx-a").json()
    second = upload_as(client, headers, "xlsx-b").json()
    expire(clock)
    plan = plan_for(tmp_path, clock)
    selected = UUID(first["file"]["file_id"])
    _, approval_id = approve(tmp_path, clock, plan, [selected])

    receipt = cleanup_for(tmp_path, clock).execute(approval_id)

    assert receipt.removed_files == 1
    assert receipt.already_removed_files == 0
    assert receipt.reclaimed_source_bytes == len(RAW)
    assert receipt.remaining_files == 1
    assert receipt.remaining_source_bytes == len(RAW)
    assert receipt.safe_erasure is False
    assert receipt.evidence_artifacts_removed is False
    assert receipt.destructive_actions_performed is True
    assert receipt.requested_files == (selected,)
    assert receipt.items[0].outcome == "removed"
    assert receipt.items[0].status == "staged"
    assert receipt.items[0].snapshot_hash is None
    assert count_rows(tmp_path / "xlsx.db") == 1
    with sqlite3.connect(tmp_path / "xlsx.db") as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM xlsx_uploads WHERE file_id=?",
                (second["file"]["file_id"],),
            ).fetchone()[0]
            == 1
        )
        payload = db.execute(
            "SELECT body FROM xlsx_source_tombstones WHERE file_id=?", (str(selected),)
        ).fetchone()[0]
    tombstone = XlsxSourceTombstone.model_validate_json(payload)
    assert tombstone.file_id == selected
    assert tombstone.task_id == UUID(first["file"]["task_id"])
    assert tombstone.approval_id == approval_id
    assert tombstone.inventory_digest == plan.inventory_digest
    assert tombstone.source_hash == first["file"]["source_hash"]
    assert tombstone.source_bytes == len(RAW)
    body = json.dumps(
        {"receipt": receipt.model_dump(mode="json"), "tombstone": json.loads(payload)},
        ensure_ascii=False,
    )
    for secret in ("alpha", "amount", "flag", "Sheet1", "xlsx-a", "xlsx-b"):
        assert secret not in body


def test_cleanup_refuses_ineligible_or_drifted_selections_before_consuming(tmp_path):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60)
    _, headers = credentials(service.broker)
    client = client_for(service)
    first = upload_as(client, headers, "xlsx-a").json()
    plan = plan_for(tmp_path, clock)
    store, retained = approve(tmp_path, clock, plan, [first["file"]["file_id"]])
    with pytest.raises(XlsxCleanupRefused, match="xlsx_cleanup_selection_not_eligible"):
        cleanup_for(tmp_path, clock).execute(retained)
    assert store.inspect(retained).consumed is False

    expire(clock)
    plan = plan_for(tmp_path, clock)
    store, drifted = approve(tmp_path, clock, plan, [first["file"]["file_id"]])
    assert upload_as(client, headers, "xlsx-b").status_code == 201
    with pytest.raises(XlsxCleanupRefused, match="xlsx_cleanup_plan_changed"):
        cleanup_for(tmp_path, clock).execute(drifted)
    assert store.inspect(drifted).consumed is False
    assert count_rows(tmp_path / "xlsx.db") == 2


def test_cleanup_refuses_unknown_or_mismatched_approvals(tmp_path):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60)
    _, headers = credentials(service.broker)
    client = client_for(service)
    first = upload_as(client, headers, "xlsx-a").json()
    expire(clock)
    plan = plan_for(tmp_path, clock)
    cleanup = cleanup_for(tmp_path, clock)
    with pytest.raises(XlsxCleanupRefused, match="xlsx_cleanup_approval_unknown"):
        cleanup.execute(uuid4())
    _, wrong_tool = approve(
        tmp_path,
        clock,
        plan,
        [first["file"]["file_id"]],
        mutate=lambda fields: {**fields, "tool_name": "xlsx.maintenance.other"},
    )
    with pytest.raises(XlsxCleanupRefused, match="xlsx_cleanup_approval_not_granted"):
        cleanup.execute(wrong_tool)
    _, extra = approve(
        tmp_path,
        clock,
        plan,
        [first["file"]["file_id"]],
        mutate=lambda fields: {
            **fields,
            "arguments": {**fields["arguments"], "all": True},
        },
    )
    with pytest.raises(XlsxCleanupRefused, match="xlsx_cleanup_selection_invalid"):
        cleanup.execute(extra)
    _, pending = approve(tmp_path, clock, plan, [first["file"]["file_id"]], approve_it=False)
    with pytest.raises(XlsxCleanupRefused, match="xlsx_cleanup_approval_not_granted"):
        cleanup.execute(pending)
    assert count_rows(tmp_path / "xlsx.db") == 1


def test_cleanup_replays_idempotently_without_touching_the_store(tmp_path):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60)
    _, headers = credentials(service.broker)
    client = client_for(service)
    first = upload_as(client, headers, "xlsx-a").json()
    expire(clock)
    plan = plan_for(tmp_path, clock)
    _, approval_id = approve(tmp_path, clock, plan, [first["file"]["file_id"]])

    first_receipt = cleanup_for(tmp_path, clock).execute(approval_id)
    after_first = (tmp_path / "xlsx.db").read_bytes()
    second_receipt = cleanup_for(tmp_path, clock).execute(approval_id)

    assert (tmp_path / "xlsx.db").read_bytes() == after_first
    assert second_receipt.already_removed_files == 1
    assert second_receipt.removed_files == 0
    assert second_receipt.reclaimed_source_bytes == 0
    assert second_receipt.receipt_digest == first_receipt.receipt_digest
    with sqlite3.connect(tmp_path / "xlsx.db") as db:
        assert db.execute("SELECT COUNT(*) FROM xlsx_source_tombstones").fetchone()[0] == 1


def test_cleanup_rolls_back_the_whole_transaction_and_can_retry(tmp_path, monkeypatch):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60)
    _, headers = credentials(service.broker)
    client = client_for(service)
    first = upload_as(client, headers, "xlsx-a").json()
    expire(clock)
    plan = plan_for(tmp_path, clock)
    store, approval_id = approve(tmp_path, clock, plan, [first["file"]["file_id"]])

    def fail(*args, **kwargs):
        raise XlsxCleanupUnavailable("xlsx_cleanup_store_unreadable")

    monkeypatch.setattr(XlsxSourceCleanup, "_remaining", staticmethod(fail))
    with pytest.raises(XlsxCleanupUnavailable, match="xlsx_cleanup_store_unreadable"):
        cleanup_for(tmp_path, clock).execute(approval_id)
    assert count_rows(tmp_path / "xlsx.db") == 1
    with sqlite3.connect(tmp_path / "xlsx.db") as db:
        assert (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='xlsx_source_tombstones'"
            ).fetchone()
            is None
        )
    assert store.inspect(approval_id).consumed is True

    monkeypatch.undo()
    receipt = cleanup_for(tmp_path, clock).execute(approval_id)
    assert receipt.removed_files == 1
    assert count_rows(tmp_path / "xlsx.db") == 0


def test_cleanup_refuses_conflicting_tombstones_and_bad_keys(tmp_path):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60)
    _, headers = credentials(service.broker)
    client = client_for(service)
    first = upload_as(client, headers, "xlsx-a").json()
    expire(clock)
    plan = plan_for(tmp_path, clock)
    _, approval_id = approve(tmp_path, clock, plan, [first["file"]["file_id"]])
    with sqlite3.connect(tmp_path / "xlsx.db") as db:
        db.execute(
            "CREATE TABLE xlsx_source_tombstones (file_id TEXT PRIMARY KEY, body TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO xlsx_source_tombstones VALUES (?,?)",
            (first["file"]["file_id"], '{"file_id":"broken"}'),
        )
    with pytest.raises(XlsxCleanupConflict, match="xlsx_cleanup_tombstone_conflict"):
        cleanup_for(tmp_path, clock).execute(approval_id)
    assert count_rows(tmp_path / "xlsx.db") == 1

    with pytest.raises(PermissionError, match="xlsx_maintenance_key_mismatch"):
        cleanup_for(tmp_path, clock, key=b"j" * 32).execute(approval_id)


def test_cleanup_requires_the_released_store_and_tombstone_layout(tmp_path):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60)
    _, headers = credentials(service.broker)
    client = client_for(service)
    upload_as(client, headers, "xlsx-a")
    expire(clock)
    plan = plan_for(tmp_path, clock)
    _, approval_id = approve(tmp_path, clock, plan, [plan.files[0].file_id])
    with sqlite3.connect(tmp_path / "xlsx.db") as db:
        db.execute("PRAGMA user_version=2")
    with pytest.raises(XlsxCleanupUnavailable, match="xlsx_maintenance_schema_unsupported"):
        cleanup_for(tmp_path, clock).execute(approval_id)
    with sqlite3.connect(tmp_path / "xlsx.db") as db:
        db.execute("PRAGMA user_version=1")
        db.execute("CREATE TABLE xlsx_source_tombstones (file_id TEXT PRIMARY KEY)")
    with pytest.raises(XlsxCleanupUnavailable, match="xlsx_cleanup_store_unavailable"):
        cleanup_for(tmp_path, clock).execute(approval_id)


def test_cleanup_cli_emits_a_receipt_and_exit_codes(tmp_path, capsys):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60)
    _, headers = credentials(service.broker)
    client = client_for(service)
    first = upload_as(client, headers, "xlsx-a").json()
    expire(clock)
    plan = plan_for(tmp_path, clock)
    _, approval_id = approve(tmp_path, clock, plan, [first["file"]["file_id"]])
    path = key_file(tmp_path)
    common = [
        "--store",
        str(tmp_path / "xlsx.db"),
        "--key-file",
        str(path),
        "--approval-db",
        str(tmp_path / "approvals.db"),
    ]
    assert load_key_file(path) == KEY
    assert main(common + ["--approval-id", str(approval_id)]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["removed_files"] == 1
    assert receipt["safe_erasure"] is False
    assert main(common + ["--approval-id", str(uuid4())]) == 2
    assert "xlsx_cleanup_approval_unknown" in capsys.readouterr().err
    _, unused = approve(tmp_path, clock, plan, [first["file"]["file_id"]])
    missing = tmp_path / "absent.db"
    assert (
        main(
            [
                "--store",
                str(missing),
                "--key-file",
                str(path),
                "--approval-db",
                str(tmp_path / "approvals.db"),
                "--approval-id",
                str(unused),
            ]
        )
        == 4
    )
    assert "xlsx_maintenance_store_unavailable" in capsys.readouterr().err


def test_cleanup_contracts_bind_the_approved_facts(tmp_path):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60)
    _, headers = credentials(service.broker)
    client = client_for(service)
    first = upload_as(client, headers, "xlsx-a").json()
    expire(clock)
    plan = plan_for(tmp_path, clock)
    _, approval_id = approve(tmp_path, clock, plan, [first["file"]["file_id"]])
    receipt = cleanup_for(tmp_path, clock).execute(approval_id)
    dumped = receipt.model_dump(mode="json")
    with pytest.raises(ValidationError, match="xlsx_cleanup_receipt_digest_mismatch"):
        XlsxCleanupReceipt.model_validate({**dumped, "receipt_digest": "sha256:" + "1" * 64})
    with pytest.raises(ValidationError, match="xlsx_cleanup_receipt_totals_invalid"):
        XlsxCleanupReceipt.model_validate({**dumped, "removed_files": 0})
    item = receipt.items[0].model_dump(mode="json")
    with pytest.raises(ValidationError, match="xlsx_cleanup_item_invalid"):
        type(receipt.items[0]).model_validate({**item, "snapshot_hash": "sha256:" + "2" * 64})
    payload = tombstone_payload(
        file_id=receipt.items[0].file_id,
        task_id=receipt.items[0].task_id,
        owner_ref=receipt.items[0].owner_ref,
        status="staged",
        snapshot_hash=None,
        source_hash=receipt.items[0].source_hash,
        source_bytes=receipt.items[0].source_bytes,
        approval_id=approval_id,
        inventory_digest=plan.inventory_digest,
    )
    assert tombstone_digest(payload).startswith("sha256:")
    with pytest.raises(ValidationError, match="xlsx_cleanup_tombstone_invalid"):
        XlsxSourceTombstone(
            **{
                **payload,
                "snapshot_hash": "sha256:" + "2" * 64,
                "removed_at": clock(),
                "tombstone_digest": tombstone_digest(payload),
            }
        )
    with pytest.raises(ValidationError, match="xlsx_cleanup_tombstone_digest_mismatch"):
        XlsxSourceTombstone(
            **payload,
            removed_at=clock(),
            tombstone_digest="sha256:" + "3" * 64,
        )
