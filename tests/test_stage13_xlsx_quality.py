import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest
from fastapi.testclient import TestClient
from test_metric_analysis import actor
from test_stage11_task_submission import credentials
from test_stage13_xlsx_conformance import COLUMNS, prepared
from test_stage13_xlsx_intake import build_service, declaration_body, declare
from test_stage13_xlsx_preflight import package
from test_stage13_xlsx_preview import entries_for, number_cell, row, sheet, text_cell
from test_stage13_xlsx_snapshot import snapshot

from generaldataagent.api.app import create_app
from generaldataagent.data_engineering import DataEngineeringRuntime
from generaldataagent.datasets.xlsx_quality import XlsxQualityService
from generaldataagent.policy.budgets import TaskBudgetLedger


def setup(tmp_path, **kwargs):
    intake = build_service(tmp_path, **kwargs)
    service = XlsxQualityService(
        intake=intake,
        budgets=TaskBudgetLedger(tmp_path / "budgets.db", clock=intake.store.clock),
        artifact_root=tmp_path / "artifacts",
        artifact_index=tmp_path / "artifact-index.db",
        scratch_root=tmp_path / "scratch",
    )
    client = TestClient(
        create_app(session_broker=intake.broker, xlsx_intake=intake, xlsx_quality=service)
    )
    token, headers = credentials(intake.broker)
    return intake, client, token, headers


def request_quality(client, headers, file_id, snapshot_hash, evidence=None, *, intent=None):
    route = f"/v1/analysis/files/xlsx/{file_id}/snapshot/quality"
    kwargs = {}
    if evidence is not None:
        route += "/verify"
        kwargs["json"] = evidence
    request_headers = {key: value for key, value in headers.items() if key != "idempotency-key"}
    if intent is not None:
        request_headers["idempotency-key"] = intent
    return client.post(
        route, params={"snapshot_hash": snapshot_hash}, headers=request_headers, **kwargs
    )


def read_receipt(client, headers, file_id, digest):
    return client.get(
        f"/v1/analysis/files/xlsx/{file_id}/snapshot/quality/receipt",
        params={"snapshot_hash": digest},
        headers=headers,
    )


def test_xlsx_quality_receipt_survives_restart_and_concurrent_replay(tmp_path, monkeypatch):
    intake, client, _, headers = setup(tmp_path)
    file_id, declared = prepared(client, headers)
    digest = snapshot(client, headers, file_id).json()["snapshot_hash"]
    intent = "quality-original-intent"
    headers = {**headers, "idempotency-key": intent}
    assert read_receipt(client, headers, file_id, digest).json()["state"] == "missing"
    ready, resume = Event(), Event()
    original = DataEngineeringRuntime.check_quality

    def delayed(runtime, **kwargs):
        ready.set()
        assert resume.wait(timeout=5)
        return original(runtime, **kwargs)

    monkeypatch.setattr(DataEngineeringRuntime, "check_quality", delayed)
    with ThreadPoolExecutor(max_workers=1) as worker:
        submitted = worker.submit(request_quality, client, headers, file_id, digest, intent=intent)
        try:
            assert ready.wait(timeout=5)
            pending = read_receipt(client, headers, file_id, digest).json()
            assert pending["state"] == "pending"
            assert pending["result"] is None
            assert (
                request_quality(client, headers, file_id, digest, intent=intent).status_code == 409
            )
        finally:
            resume.set()
        delivered = submitted.result()
    assert delivered.status_code == 200, delivered.text
    assessment = delivered.json()
    assert assessment["assessment"]["task_id"] == pending["task_id"]

    _, restarted, _, new_headers = setup(tmp_path)
    new_headers = {**new_headers, "idempotency-key": intent}
    recovered = read_receipt(restarted, new_headers, file_id, digest).json()
    assert recovered["state"] == "completed"
    assert recovered["result"] == assessment
    assert (
        request_quality(restarted, new_headers, file_id, digest, intent=intent).json() == assessment
    )
    assert len(list((tmp_path / "artifacts").glob("*/*.json"))) == 1
    other = actor().model_copy(update={"user_id": "other"})
    _, other_headers = credentials(intake.broker, other)
    assert (
        read_receipt(
            client, {**other_headers, "idempotency-key": intent}, file_id, digest
        ).status_code
        == 404
    )

    assert (
        declare(
            client, headers, file_id, declaration_body(declared, columns=[COLUMNS[0]])
        ).status_code
        == 200
    )
    new_digest = snapshot(client, headers, file_id).json()["snapshot_hash"]
    assert read_receipt(client, headers, file_id, digest).status_code == 409
    assert read_receipt(client, headers, file_id, new_digest).json()["detail"] == (
        "xlsx_quality_intent_request_changed"
    )
    assert request_quality(client, headers, file_id, new_digest, intent=intent).status_code == 409


def test_xlsx_quality_runs_existing_tool_and_verifies_same_task_evidence(tmp_path):
    _, client, _, headers = setup(tmp_path)
    file_id, _ = prepared(client, headers)
    retained = snapshot(client, headers, file_id).json()
    version = client.get(f"/v1/analysis/files/xlsx/{file_id}/snapshot/quality", headers=headers)
    assert version.status_code == 200, version.text
    assert version.json() == {
        "schema_version": "1.0",
        "file_id": file_id,
        "source_task_id": version.json()["source_task_id"],
        "interpretation_hash": retained["interpretation_hash"],
        "snapshot_hash": retained["snapshot_hash"],
    }
    assert list((tmp_path / "scratch").iterdir()) == []
    assert not (tmp_path / "artifacts").exists()
    before = (tmp_path / "xlsx.db").read_bytes()
    response = request_quality(client, headers, file_id, retained["snapshot_hash"])
    assert response.status_code == 200, response.text
    result = response.json()
    assessment, evidence = result["assessment"], result["evidence"]
    assert evidence["task_id"] == assessment["task_id"]
    assert evidence["artifact_type"] == "xlsx_snapshot_quality"
    source = assessment["source"]
    assert source["source_task_id"] == version.json()["source_task_id"]
    assert source["file_id"] == file_id
    for key in ("source_hash", "preview_hash", "confirmation_hash", "interpretation_hash"):
        assert source[key] == retained[key]
    assert source["snapshot_hash"] == retained["snapshot_hash"]
    assert (source["data_rows"], source["duplicate_row_count"]) == (7, 1)
    assert [row["violation_count"] for row in assessment["quality"]["checks"]] == [1, 3, 1]
    assert assessment["passed"] is False
    evidence_path = tmp_path / "artifacts" / evidence["task_id"] / f"{evidence['artifact_id']}.json"
    assert json.loads(evidence_path.read_bytes()) == assessment
    assert request_quality(
        client, headers, file_id, retained["snapshot_hash"], evidence
    ).json() == (result)
    assert "alpha" not in response.text
    assert "header" not in response.text
    assert list((tmp_path / "scratch").iterdir()) == []
    assert (tmp_path / "xlsx.db").read_bytes() == before
    events = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert any(
        event["task_id"] == evidence["task_id"]
        and event["event_type"] == "policy.decision"
        and event["payload"]["decision"] == "allow"
        for event in events
    )
    evidence_path.write_text("{}")
    assert (
        request_quality(client, headers, file_id, retained["snapshot_hash"], evidence).status_code
        == 409
    )


def test_xlsx_quality_preserves_exact_decimal_text_and_applies_duplicate_policy(
    tmp_path, monkeypatch
):
    _, client, _, headers = setup(tmp_path)
    exact = "123456789012345678.000000000000000001"
    raw = package(
        entries_for(
            sheet(
                row(1, text_cell("A1", "amount")),
                row(2, number_cell("A2", exact)),
                row(3, number_cell("A3", exact)),
            )
        )
    )
    file_id, declared = prepared(
        client, headers, raw=raw, columns=[{"index": 0, "header": "amount", "type": "decimal"}]
    )
    original = DataEngineeringRuntime.check_quality

    def assert_storage(runtime, **kwargs):
        connector = runtime.connectors[kwargs["connector_id"]]
        assert connector.fetch_all("SELECT column_0, typeof(column_0) FROM declared_xlsx") == (
            (exact, "text"),
            (exact, "text"),
        )
        return original(runtime, **kwargs)

    monkeypatch.setattr(DataEngineeringRuntime, "check_quality", assert_storage)
    digest = snapshot(client, headers, file_id).json()["snapshot_hash"]
    result = request_quality(client, headers, file_id, digest).json()["assessment"]
    assert result["quality"]["passed"] is True
    assert result["passed"] is False
    revision = declaration_body(
        declared,
        columns=[{"index": 0, "header": "amount", "type": "decimal"}],
        duplicate_rows="keep-all",
    )
    assert declare(client, headers, file_id, revision).status_code == 200
    new_hash = snapshot(client, headers, file_id).json()["snapshot_hash"]
    result = request_quality(client, headers, file_id, new_hash).json()["assessment"]
    assert result["passed"] is True
    assert result["source"]["data_rows"] == 2


def test_xlsx_quality_revalidates_creator_snapshot_and_retention(tmp_path):
    now = [datetime.now(UTC)]
    intake, client, token, headers = setup(tmp_path, clock=lambda: now[0], retention=60)
    file_id, declared = prepared(client, headers)
    digest = snapshot(client, headers, file_id).json()["snapshot_hash"]
    initial = request_quality(client, headers, file_id, digest).json()
    other = actor().model_copy(update={"user_id": "other"})
    _, other_headers = credentials(intake.broker, other)
    assert request_quality(client, other_headers, file_id, digest).status_code == 404
    version_path = f"/v1/analysis/files/xlsx/{file_id}/snapshot/quality"
    assert client.get(version_path, headers=other_headers).status_code == 404
    revision = declaration_body(declared, columns=[COLUMNS[0]])
    assert declare(client, headers, file_id, revision).status_code == 200
    assert client.get(version_path, headers=headers).status_code == 409
    assert request_quality(client, headers, file_id, digest, initial["evidence"]).status_code == 409
    updated = snapshot(client, headers, file_id).json()["snapshot_hash"]
    assert (
        request_quality(client, headers, file_id, updated, initial["evidence"]).status_code == 409
    )
    current = request_quality(client, headers, file_id, updated).json()
    intake.broker.revoke(token)
    assert request_quality(client, headers, file_id, updated, current["evidence"]).status_code in (
        401,
        403,
    )
    _, headers = credentials(intake.broker)
    now[0] += timedelta(seconds=61)
    assert (
        request_quality(client, headers, file_id, updated, current["evidence"]).status_code == 404
    )


@pytest.mark.parametrize("failure", ["audit", "revision"])
def test_xlsx_quality_discards_evidence_if_delivery_aborts(tmp_path, monkeypatch, failure):
    intake, client, _, headers = setup(tmp_path)
    file_id, declared = prepared(client, headers)
    digest = snapshot(client, headers, file_id).json()["snapshot_hash"]
    emitter = intake.audit.emit

    def fail_before_delivery(event):
        if event.event_type == "xlsx.snapshot_quality_prepared":
            if failure == "audit":
                raise OSError("audit unavailable")
            revision = declaration_body(declared, columns=[COLUMNS[0]])
            assert declare(client, headers, file_id, revision).status_code == 200
        emitter(event)

    monkeypatch.setattr(intake.audit, "emit", fail_before_delivery)
    intent = "failed-quality-intent"
    headers = {**headers, "idempotency-key": intent}
    response = request_quality(client, headers, file_id, digest, intent=intent)
    assert response.status_code == (503 if failure == "audit" else 409)
    assert list((tmp_path / "artifacts").glob("*/*.json")) == []
    assert list((tmp_path / "scratch").iterdir()) == []
    if failure == "audit":
        assert read_receipt(client, headers, file_id, digest).json()["state"] == "failed"
        assert request_quality(client, headers, file_id, digest, intent=intent).status_code == 409
