"""Synthetic retained CSV store -> reviewed plan -> one-time approval -> tombstoned removal."""

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from generaldataagent.artifacts.governed import GovernedArtifactStore
from generaldataagent.contracts import ActorContext, RiskLevel, ToolRequest
from generaldataagent.datasets.csv_cleanup import CsvSourceCleanup
from generaldataagent.datasets.csv_intake import CsvIntakeService
from generaldataagent.datasets.csv_intake_contracts import ConfirmCsvText
from generaldataagent.datasets.csv_intake_store import CsvUploadStore
from generaldataagent.datasets.csv_maintenance import CsvMaintenanceInventory
from generaldataagent.observability import JsonlAuditSink
from generaldataagent.policy.approvals import SQLiteApprovalStore
from generaldataagent.policy.budgets import TaskBudgetLedger
from generaldataagent.security.identity import SQLiteSessionBroker

KEY = b"i" * 32
SAMPLE = b"order_id,amount,note\r\n001,10,\r\n002,2,=1+1\r\n"
START = datetime(2026, 11, 1, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now


def service_for(root, clock):
    broker = SQLiteSessionBroker(root / "sessions.db", clock=clock)
    store = CsvUploadStore(root / "uploads.db", encryption_key=KEY, clock=clock)
    service = CsvIntakeService(
        store=store,
        broker=broker,
        budgets=TaskBudgetLedger(root / "budgets.db", clock=clock),
        artifact_root=root / "content",
        artifact_index=root / "artifact-index.db",
        scratch_root=root / "scratch",
        audit=JsonlAuditSink(root / "audit.jsonl"),
        retention_seconds=60,
        monotonic_clock=lambda: 0.0,
    )
    token = broker.issue(
        user_id="demo",
        tenant_id="demo",
        project_id="demo",
        data_domains=frozenset({"commerce"}),
        permissions=frozenset({"analysis:run"}),
        ttl_seconds=900,
    )
    return service, token


def profile(service, token, owner, key, clock):
    preview = service.stage(SAMPLE, owner, token, key)
    clock.now = START + timedelta(seconds=30)
    result = service.confirm(
        preview.file.file_id,
        owner,
        token,
        ConfirmCsvText(
            confirmed=True,
            source_hash=preview.file.source_hash,
            preview_hash=preview.file.preview_hash,
        ),
    )
    return preview, result


def request_approval(root, clock, plan, file_id):
    store = SQLiteApprovalStore(root / "approvals.db", clock=clock)
    request = ToolRequest(
        task_id=file_id,
        user_id="demo",
        tenant_id="demo",
        project_id="demo",
        agent_role="data_engineer",
        tool_name="csv.maintenance.remove",
        action="remove-expired-source",
        resource=plan.inventory_digest,
        arguments={
            "inventory_digest": plan.inventory_digest,
            "file_ids": [str(file_id)],
        },
        data_domains=("commerce",),
        requested_risk=RiskLevel.R2,
    )
    created = store.create_request(
        request,
        impact_scope="Remove one reviewed expired private CSV source",
        cost_estimate="One local transactional delete",
        classification="internal",
        rollback_plan="A content-free tombstone remains; the row is not restored automatically",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    store.approve(created.approval_id, approver_id="independent-reviewer")
    return store, created.approval_id


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def demonstrate(root):
    owner = ActorContext(
        user_id="demo", tenant_id="demo", project_id="demo", data_domains=("commerce",)
    )
    clock = Clock()
    service, token = service_for(root, clock)
    profiled, confirmed = profile(service, token, owner, "profiled", clock)
    staged = service.stage(SAMPLE, owner, token, "staged")

    key_file = root / "cleanup.key"
    key_file.write_bytes(base64.b64encode(KEY))
    key_file.chmod(0o600)

    artifacts = GovernedArtifactStore(
        root / "content",
        index_path=root / "artifact-index.db",
        actor=owner,
        retention_seconds=60,
        clock=clock,
    )
    evidence_before = {ref.artifact_id: artifacts.read_bytes(ref) for ref in confirmed.evidence}

    clock.now = START + timedelta(seconds=61)
    plan = CsvMaintenanceInventory(
        store_path=root / "uploads.db",
        encryption_key=KEY,
        artifact_index=root / "artifact-index.db",
        clock=clock,
    ).inventory()
    assert plan.status == "complete"
    assert profiled.file.file_id in {item.file_id for item in plan.files}

    _, approval_id = request_approval(root, clock, plan, profiled.file.file_id)
    cleanup = CsvSourceCleanup(
        store_path=root / "uploads.db",
        encryption_key=KEY,
        artifact_index=root / "artifact-index.db",
        approval_db=root / "approvals.db",
        clock=clock,
    )
    receipt = cleanup.execute(approval_id)
    replay = cleanup.execute(approval_id)

    assert receipt.removed_files == 1 and receipt.already_removed_files == 0
    assert replay.removed_files == 0 and replay.already_removed_files == 1
    assert receipt.receipt_digest == replay.receipt_digest
    assert receipt.remaining_files == 1
    first_evidence = confirmed.evidence[0]
    assert artifacts.read_bytes(first_evidence) == evidence_before[first_evidence.artifact_id]
    assert receipt.evidence_artifacts_removed is False
    assert receipt.safe_erasure is False
    assert staged.file.file_id != profiled.file.file_id

    return {
        "reviewed_plan_digest": plan.inventory_digest,
        "approval_id": str(approval_id),
        "selected_file_id": str(profiled.file.file_id),
        "removed_files": receipt.removed_files,
        "reclaimed_source_bytes": receipt.reclaimed_source_bytes,
        "remaining_files": receipt.remaining_files,
        "idempotent_replay": replay.already_removed_files == 1,
        "replay_receipt_digest_matches": receipt.receipt_digest == replay.receipt_digest,
        "unselected_staged_file_retained": True,
        "evidence_artifact_readable_after_cleanup": True,
        "evidence_artifacts_removed": receipt.evidence_artifacts_removed,
        "safe_erasure": receipt.safe_erasure,
        "destructive_actions_performed": receipt.destructive_actions_performed,
        "model_calls": 0,
    }


if __name__ == "__main__":
    with TemporaryDirectory(prefix="gda-csv-cleanup-") as directory:
        print(json.dumps(demonstrate(Path(directory)), indent=2))
