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
from generaldataagent.contracts import RiskLevel, ToolRequest
from generaldataagent.datasets.csv_cleanup import (
    CsvCleanupConflict,
    CsvCleanupRefused,
    CsvCleanupUnavailable,
    CsvSourceCleanup,
    main,
)
from generaldataagent.datasets.csv_cleanup_contracts import (
    CsvCleanupReceipt,
    CsvSourceTombstone,
    tombstone_digest,
    tombstone_payload,
)
from generaldataagent.datasets.csv_intake_store import CsvIntakeConflict
from generaldataagent.datasets.csv_maintenance import CsvMaintenanceInventory, load_key_file
from generaldataagent.policy.approvals import SQLiteApprovalStore

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


def cleanup_for(tmp_path, clock):
    return CsvSourceCleanup(
        store_path=tmp_path / "csv.db",
        encryption_key=KEY,
        artifact_index=tmp_path / "artifact-index.db",
        approval_db=tmp_path / "approvals.db",
        clock=clock,
    )


def stage(service, token, key="file"):
    return service.stage(RAW, actor(), token, key)


def profile(service, token, key="profile"):
    preview = service.stage(RAW, actor(), token, key)
    result = service.confirm(preview.file.file_id, actor(), token, confirmation(preview))
    return preview, result


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
        "tool_name": "csv.maintenance.remove",
        "action": "remove-expired-source",
        "resource": plan.inventory_digest,
        "arguments": arguments,
        "data_domains": ("commerce",),
        "requested_risk": RiskLevel.R2,
    }
    if mutate is not None:
        fields = mutate(fields)
    request = ToolRequest(**fields)
    created = store.create_request(
        request,
        impact_scope="Remove selected expired private CSV sources",
        cost_estimate="One local transactional delete",
        classification="internal",
        rollback_plan="Tombstones retain facts; source rows are not restored automatically",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    if approve_it:
        store.approve(created.approval_id, approver_id="independent-reviewer")
    return store, created.approval_id


def only_id(plan):
    assert len(plan.files) == 1
    return plan.files[0].file_id


def tombstone_for(entry, *, approval_id, inventory_digest, removed_at):
    payload = tombstone_payload(
        file_id=entry.file_id,
        task_id=entry.task_id,
        owner_ref=entry.owner_ref,
        source_hash=entry.source_hash,
        source_bytes=entry.source_bytes,
        report_digest=entry.report_digest,
        evidence_artifact_ids=entry.evidence_artifact_ids,
        approval_id=approval_id,
        inventory_digest=inventory_digest,
    )
    return CsvSourceTombstone(
        **payload, removed_at=removed_at, tombstone_digest=tombstone_digest(payload)
    )


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_approved_cleanup_removes_only_the_selected_expired_source(tmp_path):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60)
    token = credentials_token(service)
    first = stage(service, token, "first")
    second = stage(service, token, "second")
    clock.now = NOW + timedelta(seconds=61)
    plan = inventory_for(tmp_path, clock).inventory()
    assert plan.status == "complete"
    store, approval_id = approve(tmp_path, clock, plan, [first.file.file_id])

    receipt = cleanup_for(tmp_path, clock).execute(approval_id)

    assert receipt.removed_files == 1
    assert receipt.already_removed_files == 0
    assert receipt.reclaimed_source_bytes == len(RAW)
    assert receipt.remaining_files == 1
    assert receipt.remaining_source_bytes == len(RAW)
    assert receipt.safe_erasure is False
    assert receipt.evidence_artifacts_removed is False
    assert receipt.destructive_actions_performed is True
    assert receipt.requested_files == (first.file.file_id,)
    assert receipt.items[0].outcome == "removed"
    assert receipt.items[0].source_hash == first.file.source_hash
    assert not (tmp_path / "artifact-index.db").exists()
    with sqlite3.connect(tmp_path / "csv.db") as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM csv_uploads WHERE file_id=?", (str(first.file.file_id),)
            ).fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM csv_uploads WHERE file_id=?", (str(second.file.file_id),)
            ).fetchone()[0]
            == 1
        )
        payload = db.execute(
            "SELECT body FROM csv_source_tombstones WHERE file_id=?", (str(first.file.file_id),)
        ).fetchone()[0]
    tombstone = CsvSourceTombstone.model_validate_json(payload)
    assert tombstone.file_id == first.file.file_id
    assert tombstone.task_id == first.file.task_id
    assert tombstone.approval_id == approval_id
    assert tombstone.inventory_digest == plan.inventory_digest
    assert tombstone.source_hash == first.file.source_hash
    assert "amount" not in payload and str(tmp_path) not in payload
    assert store.inspect(approval_id).consumed is True

    after = inventory_for(tmp_path, clock).inventory()
    assert {item.file_id for item in after.files} == {second.file.file_id}
    assert after.summary.retained_source_bytes == len(RAW)


def test_cleanup_is_idempotent_and_keeps_one_tombstone_per_file(tmp_path):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60)
    token = credentials_token(service)
    staged = stage(service, token)
    clock.now = NOW + timedelta(seconds=61)
    plan = inventory_for(tmp_path, clock).inventory()
    _, approval_id = approve(tmp_path, clock, plan, [staged.file.file_id])

    first = cleanup_for(tmp_path, clock).execute(approval_id)
    second = cleanup_for(tmp_path, clock).execute(approval_id)

    assert (first.removed_files, first.already_removed_files) == (1, 0)
    assert (second.removed_files, second.already_removed_files) == (0, 1)
    assert second.reclaimed_source_bytes == 0
    assert second.remaining_files == first.remaining_files
    assert second.receipt_digest == first.receipt_digest
    with sqlite3.connect(tmp_path / "csv.db") as db:
        assert db.execute("SELECT COUNT(*) FROM csv_source_tombstones").fetchone()[0] == 1


def test_cleanup_resumes_after_approval_consumption_before_deletion(tmp_path):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60)
    token = credentials_token(service)
    staged = stage(service, token)
    clock.now = NOW + timedelta(seconds=61)
    plan = inventory_for(tmp_path, clock).inventory()
    store, approval_id = approve(tmp_path, clock, plan, [staged.file.file_id])
    store.consume_granted(approval_id)

    receipt = cleanup_for(tmp_path, clock).execute(approval_id)

    assert receipt.removed_files == 1
    with sqlite3.connect(tmp_path / "csv.db") as db:
        assert db.execute("SELECT COUNT(*) FROM csv_uploads").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM csv_source_tombstones").fetchone()[0] == 1


def test_plan_drift_refuses_cleanup_before_consuming_the_approval(tmp_path):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60)
    token = credentials_token(service)
    staged = stage(service, token)
    clock.now = NOW + timedelta(seconds=61)
    plan = inventory_for(tmp_path, clock).inventory()
    store, approval_id = approve(tmp_path, clock, plan, [staged.file.file_id])
    clock.now = NOW + timedelta(seconds=120)
    service.stage(RAW, actor(), token, "new")

    with pytest.raises(CsvCleanupRefused, match="plan_changed"):
        cleanup_for(tmp_path, clock).execute(approval_id)

    assert store.inspect(approval_id).consumed is False
    with sqlite3.connect(tmp_path / "csv.db") as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM csv_uploads WHERE file_id=?", (str(staged.file.file_id),)
            ).fetchone()[0]
            == 1
        )


def test_unknown_approval_is_refused(tmp_path):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60)
    token = credentials_token(service)
    stage(service, token)
    clock.now = NOW + timedelta(seconds=61)

    with pytest.raises(CsvCleanupRefused, match="approval_unknown"):
        cleanup_for(tmp_path, clock).execute(uuid4())

    with sqlite3.connect(tmp_path / "csv.db") as db:
        assert db.execute("SELECT COUNT(*) FROM csv_uploads").fetchone()[0] == 1


def test_missing_source_without_tombstone_refuses_without_consuming(tmp_path):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60)
    token = credentials_token(service)
    staged = stage(service, token)
    clock.now = NOW + timedelta(seconds=61)
    plan = inventory_for(tmp_path, clock).inventory()
    store, approval_id = approve(tmp_path, clock, plan, [staged.file.file_id])
    with sqlite3.connect(tmp_path / "csv.db") as db:
        db.execute("DELETE FROM csv_uploads")

    with pytest.raises(CsvCleanupRefused, match="plan_changed"):
        cleanup_for(tmp_path, clock).execute(approval_id)

    assert store.inspect(approval_id).consumed is False


def test_blocked_plan_refuses_all_cleanup(tmp_path):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60)
    token = credentials_token(service)
    held_preview, held_result = profile(service, token, "held")
    other = stage(service, token, "other")
    GovernedArtifactStore(
        tmp_path / "content",
        index_path=tmp_path / "artifact-index.db",
        actor=actor(),
        retention_seconds=60,
        clock=clock,
    ).set_legal_hold(held_result.evidence[0], held=True)
    clock.now = NOW + timedelta(seconds=61)
    plan = inventory_for(tmp_path, clock).inventory()
    assert plan.status == "blocked"
    _, approval_id = approve(tmp_path, clock, plan, [other.file.file_id])

    with pytest.raises(CsvCleanupRefused, match="plan_blocked"):
        cleanup_for(tmp_path, clock).execute(approval_id)

    with sqlite3.connect(tmp_path / "csv.db") as db:
        assert db.execute("SELECT COUNT(*) FROM csv_uploads").fetchone()[0] == 2
    assert other.file.file_id != held_preview.file.file_id


def test_retained_file_cannot_be_selected(tmp_path):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60)
    token = credentials_token(service)
    retained = stage(service, token)
    plan = inventory_for(tmp_path, clock).inventory()
    assert plan.files[0].review_state == "retain"
    _, approval_id = approve(tmp_path, clock, plan, [retained.file.file_id])

    with pytest.raises(CsvCleanupRefused, match="selection_not_eligible"):
        cleanup_for(tmp_path, clock).execute(approval_id)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda fields: fields | {"arguments": {"file_ids": list(fields["arguments"]["file_ids"])}},
        lambda fields: (
            fields | {"arguments": fields["arguments"] | {"inventory_digest": "sha256:" + "0" * 64}}
        ),
        lambda fields: fields | {"resource": "sha256:" + "0" * 64},
        lambda fields: fields | {"tool_name": "csv.maintenance.inventory"},
        lambda fields: fields | {"action": "delete"},
        lambda fields: (
            fields | {"arguments": fields["arguments"] | {"file_ids": [], "extra": True}}
        ),
        lambda fields: fields | {"arguments": fields["arguments"] | {"file_ids": ["not-a-uuid"]}},
    ],
)
def test_approval_must_bind_the_exact_tool_digest_and_selection(tmp_path, mutation):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60)
    token = credentials_token(service)
    staged = stage(service, token)
    clock.now = NOW + timedelta(seconds=61)
    plan = inventory_for(tmp_path, clock).inventory()
    _, approval_id = approve(tmp_path, clock, plan, [staged.file.file_id], mutate=mutation)

    with pytest.raises(CsvCleanupRefused):
        cleanup_for(tmp_path, clock).execute(approval_id)


def test_pending_rejected_or_expired_approval_cannot_clean(tmp_path):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60)
    token = credentials_token(service)
    staged = stage(service, token)
    clock.now = NOW + timedelta(seconds=61)
    plan = inventory_for(tmp_path, clock).inventory()
    store, approval_id = approve(tmp_path, clock, plan, [staged.file.file_id], approve_it=False)
    with pytest.raises(CsvCleanupRefused, match="not_granted"):
        cleanup_for(tmp_path, clock).execute(approval_id)

    store.approve(approval_id, approver_id="independent-reviewer")
    clock.now = datetime.now(UTC) + timedelta(hours=2)
    with pytest.raises(CsvCleanupRefused, match="approval_closed"):
        cleanup_for(tmp_path, clock).execute(approval_id)

    with sqlite3.connect(tmp_path / "csv.db") as db:
        assert db.execute("SELECT COUNT(*) FROM csv_uploads").fetchone()[0] == 1


def test_rejected_approval_cannot_clean(tmp_path):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60)
    token = credentials_token(service)
    staged = stage(service, token)
    clock.now = NOW + timedelta(seconds=61)
    plan = inventory_for(tmp_path, clock).inventory()
    store, approval_id = approve(tmp_path, clock, plan, [staged.file.file_id], approve_it=False)
    store.reject(approval_id, reviewer_id="reviewer", reason="not needed")

    with pytest.raises(CsvCleanupRefused, match="not_granted"):
        cleanup_for(tmp_path, clock).execute(approval_id)


@pytest.mark.parametrize("mismatch", ["other_approval", "changed_facts", "row_present"])
def test_conflicting_tombstone_blocks_the_whole_selection(tmp_path, mismatch):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60)
    token = credentials_token(service)
    staged = stage(service, token)
    clock.now = NOW + timedelta(seconds=61)
    plan = inventory_for(tmp_path, clock).inventory()
    _, approval_id = approve(tmp_path, clock, plan, [staged.file.file_id])
    entry = plan.files[0]
    tombstone = tombstone_for(
        entry,
        approval_id=uuid4() if mismatch == "other_approval" else approval_id,
        inventory_digest=plan.inventory_digest,
        removed_at=clock.now,
    )
    if mismatch == "changed_facts":
        tombstone = tombstone.model_copy(update={"source_bytes": entry.source_bytes + 1})
    with sqlite3.connect(tmp_path / "csv.db") as db:
        db.execute(
            "CREATE TABLE csv_source_tombstones (file_id TEXT PRIMARY KEY, body TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO csv_source_tombstones VALUES (?,?)",
            (str(entry.file_id), tombstone.model_dump_json()),
        )

    with pytest.raises(CsvCleanupConflict, match="tombstone_conflict"):
        cleanup_for(tmp_path, clock).execute(approval_id)

    with sqlite3.connect(tmp_path / "csv.db") as db:
        assert db.execute("SELECT COUNT(*) FROM csv_uploads").fetchone()[0] == 1


def test_profiled_source_cleanup_keeps_evidence_and_artifact_index(tmp_path):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60)
    token = credentials_token(service)
    preview = stage(service, token, "profile")
    clock.now = NOW + timedelta(seconds=30)
    service.confirm(preview.file.file_id, actor(), token, confirmation(preview))
    clock.now = NOW + timedelta(seconds=61)
    plan = inventory_for(tmp_path, clock).inventory()
    entry = plan.files[0]
    assert entry.review_state == "expired_review_required"
    assert entry.report_state == "readable" and len(entry.evidence_artifact_ids) == 2
    _, approval_id = approve(tmp_path, clock, plan, [entry.file_id])
    index_before = digest(tmp_path / "artifact-index.db")
    content_before = sorted(
        (path.name, digest(path)) for path in (tmp_path / "content").rglob("*") if path.is_file()
    )

    receipt = cleanup_for(tmp_path, clock).execute(approval_id)

    assert receipt.removed_files == 1
    assert receipt.evidence_artifacts_removed is False
    assert digest(tmp_path / "artifact-index.db") == index_before
    assert content_before == sorted(
        (path.name, digest(path)) for path in (tmp_path / "content").rglob("*") if path.is_file()
    )
    with sqlite3.connect(tmp_path / "csv.db") as db:
        tombstone = CsvSourceTombstone.model_validate_json(
            db.execute("SELECT body FROM csv_source_tombstones").fetchone()[0]
        )
    assert tombstone.evidence_artifact_ids == entry.evidence_artifact_ids
    assert tombstone.report_digest == entry.report_digest


def test_cleanup_releases_retained_capacity(tmp_path):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60, max_total=1)
    token = credentials_token(service)
    staged = stage(service, token)
    with pytest.raises(CsvIntakeConflict, match="capacity_exhausted"):
        stage(service, token, "blocked")
    clock.now = NOW + timedelta(seconds=61)
    plan = inventory_for(tmp_path, clock).inventory()
    _, approval_id = approve(tmp_path, clock, plan, [staged.file.file_id])
    cleanup_for(tmp_path, clock).execute(approval_id)

    replacement = stage(service, token, "replacement")

    assert replacement.file.file_id != staged.file.file_id


@pytest.mark.parametrize("mutation", ["wrong_key", "missing_store", "schema"])
def test_cleanup_never_creates_or_opens_unsupported_state(tmp_path, mutation):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60)
    token = credentials_token(service)
    staged = stage(service, token)
    clock.now = NOW + timedelta(seconds=61)
    plan = inventory_for(tmp_path, clock).inventory()
    _, approval_id = approve(tmp_path, clock, plan, [staged.file.file_id])
    if mutation == "wrong_key":
        port = CsvSourceCleanup(
            store_path=tmp_path / "csv.db",
            encryption_key=b"w" * 32,
            artifact_index=tmp_path / "artifact-index.db",
            approval_db=tmp_path / "approvals.db",
            clock=clock,
        )
        with pytest.raises(PermissionError, match="key_mismatch"):
            port.execute(approval_id)
        return
    if mutation == "missing_store":
        path = tmp_path / "absent.db"
        port = CsvSourceCleanup(
            store_path=path,
            encryption_key=KEY,
            artifact_index=tmp_path / "artifact-index.db",
            approval_db=tmp_path / "approvals.db",
            clock=clock,
        )
        with pytest.raises(CsvCleanupUnavailable):
            port.execute(approval_id)
        assert not path.exists()
        return
    with sqlite3.connect(tmp_path / "csv.db") as db:
        db.execute("PRAGMA user_version=2")
    with pytest.raises(CsvCleanupUnavailable):
        cleanup_for(tmp_path, clock).execute(approval_id)


def test_cli_removes_reviewed_source_and_replays_idempotently(tmp_path, capsys):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60)
    token = credentials_token(service)
    staged = stage(service, token)
    clock.now = NOW + timedelta(seconds=61)
    plan = inventory_for(tmp_path, clock).inventory()
    _, approval_id = approve(tmp_path, clock, plan, [staged.file.file_id])
    key_file = tmp_path / "cleanup.key"
    key_file.write_bytes(base64.b64encode(KEY))
    key_file.chmod(0o600)
    arguments = [
        "--store",
        str(tmp_path / "csv.db"),
        "--key-file",
        str(key_file),
        "--artifact-index",
        str(tmp_path / "artifact-index.db"),
        "--approval-db",
        str(tmp_path / "approvals.db"),
        "--approval-id",
        str(approval_id),
    ]

    assert main(arguments) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["removed_files"] == 1
    assert receipt["safe_erasure"] is False
    assert main(arguments) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay["already_removed_files"] == 1
    assert replay["receipt_digest"] == receipt["receipt_digest"]


def test_cli_exit_codes_for_unavailable_and_conflict(tmp_path, capsys):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60)
    token = credentials_token(service)
    staged = stage(service, token)
    clock.now = NOW + timedelta(seconds=61)
    plan = inventory_for(tmp_path, clock).inventory()
    _, approval_id = approve(tmp_path, clock, plan, [staged.file.file_id])
    key_file = tmp_path / "cleanup.key"
    key_file.write_bytes(base64.b64encode(KEY))
    key_file.chmod(0o600)
    base = [
        "--store",
        str(tmp_path / "csv.db"),
        "--key-file",
        str(key_file),
        "--artifact-index",
        str(tmp_path / "artifact-index.db"),
        "--approval-db",
        str(tmp_path / "approvals.db"),
    ]
    assert main(base + ["--approval-id", str(approval_id)]) == 0
    capsys.readouterr()
    assert main(base + ["--approval-id", str(approval_id)]) == 0
    capsys.readouterr()

    with sqlite3.connect(tmp_path / "csv.db") as db:
        db.execute(
            "UPDATE csv_source_tombstones SET body=?",
            (
                CsvSourceTombstone.model_validate_json(
                    db.execute("SELECT body FROM csv_source_tombstones").fetchone()[0]
                )
                .model_copy(update={"source_bytes": 1})
                .model_dump_json(),
            ),
        )
    assert main(base + ["--approval-id", str(approval_id)]) == 5
    assert "tombstone_conflict" in capsys.readouterr().err

    (tmp_path / "csv.db").rename(tmp_path / "moved.db")
    assert main(base + ["--approval-id", str(approval_id)]) == 4
    assert "store_unavailable" in capsys.readouterr().err


def test_receipt_and_tombstone_contracts_reject_tampering(tmp_path):
    clock = Clock()
    service = build_service(tmp_path, clock=clock, retention=60)
    token = credentials_token(service)
    staged = stage(service, token)
    clock.now = NOW + timedelta(seconds=61)
    plan = inventory_for(tmp_path, clock).inventory()
    _, approval_id = approve(tmp_path, clock, plan, [staged.file.file_id])
    receipt = cleanup_for(tmp_path, clock).execute(approval_id)
    payload = receipt.model_dump()
    for field, value in (
        ("removed_files", 0),
        ("reclaimed_source_bytes", 0),
        ("receipt_digest", "sha256:" + "0" * 64),
        ("items", ()),
    ):
        with pytest.raises(ValueError):
            CsvCleanupReceipt(**(payload | {field: value}))

    entry = plan.files[0]
    tombstone = tombstone_for(
        entry,
        approval_id=approval_id,
        inventory_digest=plan.inventory_digest,
        removed_at=clock.now,
    )
    with pytest.raises(ValueError):
        CsvSourceTombstone(
            **(
                tombstone.model_dump()
                | {"evidence_artifact_ids": (), "report_digest": "sha256:" + "0" * 64}
            )
        )
    with pytest.raises(ValueError, match="tombstone_digest_mismatch"):
        CsvSourceTombstone(**(tombstone.model_dump() | {"source_bytes": entry.source_bytes + 1}))
    with pytest.raises(ValueError):
        CsvSourceTombstone(**(tombstone.model_dump() | {"source": "raw"}))


def test_key_file_loading_matches_the_inventory_port(tmp_path):
    key_file = tmp_path / "cleanup.key"
    key_file.write_bytes(base64.b64encode(KEY))
    key_file.chmod(0o600)
    assert load_key_file(key_file) == KEY


def credentials_token(service):
    token, _ = credentials(service.broker)
    return token
