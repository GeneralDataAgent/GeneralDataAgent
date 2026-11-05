"""Actual synthetic retained CSV store -> read-only expired inventory -> reviewed plan digest."""

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from generaldataagent.artifacts.governed import GovernedArtifactStore
from generaldataagent.contracts import ActorContext
from generaldataagent.datasets.csv_intake import CsvIntakeService
from generaldataagent.datasets.csv_intake_contracts import ConfirmCsvText
from generaldataagent.datasets.csv_intake_store import CsvUploadStore
from generaldataagent.datasets.csv_maintenance import (
    CsvMaintenanceInventory,
    load_key_file,
)
from generaldataagent.observability import JsonlAuditSink
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


def profile(service, token, owner, key):
    preview = service.stage(SAMPLE, owner, token, key)
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


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def demonstrate(root):
    owner = ActorContext(
        user_id="demo", tenant_id="demo", project_id="demo", data_domains=("commerce",)
    )
    clock = Clock()
    service, token = service_for(root, clock)
    profile(service, token, owner, "profiled")
    held_preview, held = profile(service, token, owner, "held")
    staged = service.stage(SAMPLE, owner, token, "staged")

    GovernedArtifactStore(
        root / "content",
        index_path=root / "artifact-index.db",
        actor=owner,
        retention_seconds=60,
        clock=clock,
    ).set_legal_hold(held.evidence[0], held=True)

    key_file = root / "maintenance.key"
    key_file.write_bytes(base64.b64encode(KEY))
    key_file.chmod(0o600)
    assert load_key_file(key_file) == KEY

    before = {name: file_hash(root / name) for name in ("uploads.db", "artifact-index.db")}
    clock.now = START + timedelta(seconds=61)
    port = CsvMaintenanceInventory(
        store_path=root / "uploads.db",
        encryption_key=KEY,
        artifact_index=root / "artifact-index.db",
        clock=clock,
    )
    plan = port.inventory()
    again = port.inventory()
    assert plan.inventory_digest == again.inventory_digest
    assert before == {name: file_hash(root / name) for name in ("uploads.db", "artifact-index.db")}
    assert plan.destructive_actions_performed is False
    assert plan.status == "blocked"

    by_id = {str(item.file_id): item for item in plan.files}
    assert by_id[str(held_preview.file.file_id)].blocked_reasons == (
        "legal_hold",
        "evidence_expired",
    )
    assert by_id[str(staged.file.file_id)].review_state == "expired_review_required"
    assert '"001"' not in plan.model_dump_json() and "=1+1" not in plan.model_dump_json()

    service.stage(b"order_id,amount\n003,30\n", owner, token, "after-plan")
    changed = port.inventory()
    assert changed.inventory_digest != plan.inventory_digest

    return {
        "operator_read_only_inventory": True,
        "retained_files": plan.summary.retained_files,
        "expired_files": plan.summary.expired_files,
        "review_required_files": plan.summary.review_required_files,
        "blocked_files": plan.summary.blocked_files,
        "held_file_blocks_plan": True,
        "plan_status": plan.status,
        "stable_digest_on_repeat": plan.inventory_digest == again.inventory_digest,
        "store_and_index_unchanged": True,
        "destructive_actions_performed": plan.destructive_actions_performed,
        "drift_detected_after_new_upload": changed.inventory_digest != plan.inventory_digest,
        "csv_values_absent": True,
        "raw_source_deleted": False,
        "capacity_released": False,
        "model_calls": 0,
    }


if __name__ == "__main__":
    with TemporaryDirectory(prefix="gda-csv-maintenance-") as directory:
        print(json.dumps(demonstrate(Path(directory)), indent=2))
