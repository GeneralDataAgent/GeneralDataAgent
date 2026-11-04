"""Actual synthetic intake -> expired retained sources -> separate read-only planning CLI."""

import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from examples.csv_intake_demo import SAMPLE, service_for
from generaldataagent.contracts import ActorContext
from generaldataagent.datasets.csv_intake_contracts import ConfirmCsvText
from generaldataagent.datasets.csv_intake_store import CsvUploadStore
from generaldataagent.datasets.csv_retention import CsvRetentionInspector
from generaldataagent.observability import JsonlAuditSink
from generaldataagent.security.identity import SQLiteSessionBroker


def operator_reader():
    """Trusted disposable fixture factory. No customer data/key or production mode."""
    root = Path(os.environ["GDA_CSV_RETENTION_DEMO_ROOT"])
    if not (root / "sessions.db").is_file():
        raise ValueError("existing_demo_session_store_required")
    store = CsvUploadStore(
        root / "uploads.db",
        encryption_key=b"i" * 32,
        read_only=True,
        clock=lambda: datetime.fromisoformat(os.environ["GDA_CSV_RETENTION_DEMO_NOW"]),
    )
    return CsvRetentionInspector(
        store,
        SQLiteSessionBroker(root / "sessions.db"),
        audit=JsonlAuditSink(root / "retention-audit.jsonl"),
    )


def hashes(root):
    return {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in ("uploads.db", "budgets.db", "artifact-index.db")
    }


def demonstrate(root):
    service = service_for(root)
    now = datetime.now(UTC)
    service.store.clock = lambda: now
    service.retention_seconds = 1
    actor = ActorContext(
        user_id="demo", tenant_id="demo", project_id="demo", data_domains=("commerce",)
    )
    token = service.broker.issue(
        user_id="demo",
        tenant_id="demo",
        project_id="demo",
        data_domains=frozenset({"commerce"}),
        permissions=frozenset({"analysis:run"}),
        ttl_seconds=900,
    )
    first = service.stage(SAMPLE, actor, token, "first")
    service.confirm(
        first.file.file_id,
        actor,
        token,
        ConfirmCsvText(
            confirmed=True, source_hash=first.file.source_hash, preview_hash=first.file.preview_hash
        ),
    )
    service.stage(b"label\nsecond\n", actor, token, "second")
    now += timedelta(seconds=2)
    before = hashes(root)
    env = os.environ | {
        "GDA_CSV_RETENTION_DEMO_ROOT": str(root),
        "GDA_CSV_RETENTION_DEMO_NOW": now.isoformat(),
        "GDA_CSV_RETENTION_TOKEN": token,
    }
    command = [
        sys.executable,
        "-m",
        "generaldataagent.services.csv_retention",
        "--development",
        "--factory",
        "examples.csv_retention_demo:operator_reader",
        "--data-domain",
        "commerce",
    ]
    process = subprocess.run(command, env=env, text=True, capture_output=True, check=True)
    plan = json.loads(process.stdout)
    plan_path = root / "review-plan.json"
    plan_path.write_text(process.stdout)
    verified = subprocess.run(
        command + ["--verify-plan", str(plan_path)],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert not json.loads(verified.stdout)["deletion_authorized"]
    assert hashes(root) == before
    assert plan["retained_files"] == plan["expired_files"] == 2
    assert all(e["decision"] == "blocked_hold_unknown" for e in plan["entries"])
    try:
        service.stage(SAMPLE, actor, token, "first")
        raise AssertionError("expired key must not resurrect source")
    except PermissionError:
        pass
    assert hashes(root) == before
    service.stage(b"label\nthird\n", actor, token, "third")
    after_change = hashes(root)
    changed = subprocess.run(
        command + ["--verify-plan", str(plan_path)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert (
        changed.returncode == 3
        and json.loads(changed.stdout)["status"] == "csv_retention_plan_conflict"
    )
    assert hashes(root) == after_change
    return {
        "subprocess_cli": True,
        "retained_files": 2,
        "expired_files": 2,
        "registered_reports": sum(
            e["report_state"] == "registered_not_verified" for e in plan["entries"]
        ),
        "source_budget_index_unchanged": True,
        "expired_idempotency_preserved": True,
        "committed_change_invalidates_plan": True,
        "unknown_hold_blocks_all_expired": True,
        "deletion_authorized": False,
        "quota_released": 0,
        "real_model_calls": 0,
    }


if __name__ == "__main__":
    with TemporaryDirectory(prefix="gda-csv-retention-") as directory:
        print(json.dumps(demonstrate(Path(directory)), indent=2))
