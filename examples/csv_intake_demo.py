"""Actual synthetic CSV bytes -> ASGI preview -> reopened-state confirmation -> profile evidence."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from generaldataagent.api.app import create_app
from generaldataagent.datasets.csv_intake import CsvIntakeService
from generaldataagent.datasets.csv_intake_store import CsvUploadStore
from generaldataagent.observability import JsonlAuditSink
from generaldataagent.policy.budgets import TaskBudgetLedger
from generaldataagent.security.identity import SQLiteSessionBroker

SAMPLE = b"order_id,amount,note\r\n001,10,\r\n002,2,=1+1\r\n002,2,=1+1\r\n"


def service_for(root):
    broker = SQLiteSessionBroker(root / "sessions.db")
    return CsvIntakeService(
        store=CsvUploadStore(root / "uploads.db", encryption_key=b"i" * 32),
        broker=broker,
        budgets=TaskBudgetLedger(root / "budgets.db"),
        artifact_root=root / "content",
        artifact_index=root / "artifact-index.db",
        scratch_root=root / "scratch",
        audit=JsonlAuditSink(root / "audit.jsonl"),
    )


def client_for(service):
    token = service.broker.issue(
        user_id="demo",
        tenant_id="demo",
        project_id="demo",
        data_domains=frozenset({"commerce"}),
        permissions=frozenset({"analysis:run"}),
        ttl_seconds=900,
    )
    headers = {
        "authorization": f"Bearer {token}",
        "x-user-id": "demo",
        "x-tenant-id": "demo",
        "x-project-id": "demo",
        "x-data-domain": "commerce",
        "idempotency-key": "csv-demo",
    }
    return TestClient(
        create_app(session_broker=service.broker, csv_intake=service), headers=headers
    )


def demonstrate(root):
    sample = root / "client-selected.csv"
    sample.write_bytes(SAMPLE)
    first = service_for(root)
    with client_for(first) as client:
        response = client.post(
            "/v1/analysis/files/csv",
            content=sample.read_bytes(),
            headers={"content-type": "text/csv"},
        )
        response.raise_for_status()
        preview = response.json()
        assert preview["file"]["status"] == "staged" and not list(
            first.artifact_root.rglob("*.json")
        )
    service = service_for(root)
    with client_for(service) as client:
        url = "/v1/analysis/files/csv/" + preview["file"]["file_id"]
        assert client.get(url).json() == preview
        confirmation = {
            "confirmed": True,
            "source_hash": preview["file"]["source_hash"],
            "preview_hash": preview["file"]["preview_hash"],
        }
        response = client.post(url + "/confirm", json=confirmation)
        response.raise_for_status()
        result = response.json()
        assert client.post(url + "/confirm", json=confirmation).json() == result
        assert client.get(url + "/profile").json() == result
        assert result["profile"]["columns"][0]["minimum"] == "001"
        assert result["profile"]["columns"][1]["minimum"] == "10"  # Text, not a numeric claim.
        assert all(
            c["data_type"] == "TEXT" and c["mean"] is None for c in result["profile"]["columns"]
        )
        assert not list(service.scratch_root.iterdir())
        return {
            "asgi_api": True,
            "state_reopened": True,
            "preview_rows": preview["data"]["row_count"],
            "empty_cells": preview["data"]["empty_cells"],
            "duplicate_rows": preview["data"]["duplicate_rows"],
            "formula_like_cells": preview["data"]["formula_like_cells"],
            "profile_status": result["file"]["status"],
            "text_only": True,
            "evidence_types": sorted(r["artifact_type"] for r in result["evidence"]),
            "scratch_clean": True,
            "repeated_confirmation_same_report": True,
            "model_calls": 0,
            "certified_metric_registered": False,
        }


if __name__ == "__main__":
    with TemporaryDirectory(prefix="gda-csv-intake-") as directory:
        print(json.dumps(demonstrate(Path(directory)), indent=2))
