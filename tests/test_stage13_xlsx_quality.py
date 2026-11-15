import json
from datetime import UTC, datetime, timedelta

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


def request_quality(client, headers, file_id, snapshot_hash, evidence=None):
    route = f"/v1/analysis/files/xlsx/{file_id}/snapshot/quality"
    kwargs = {}
    if evidence is not None:
        route += "/verify"
        kwargs["json"] = evidence
    return client.post(route, params={"snapshot_hash": snapshot_hash}, headers=headers, **kwargs)


def test_xlsx_quality_runs_existing_tool_and_verifies_same_task_evidence(tmp_path):
    _, client, _, headers = setup(tmp_path)
    file_id, _ = prepared(client, headers)
    retained = snapshot(client, headers, file_id).json()
    before = (tmp_path / "xlsx.db").read_bytes()
    response = request_quality(client, headers, file_id, retained["snapshot_hash"])
    assert response.status_code == 200, response.text
    result = response.json()
    assessment, evidence = result["assessment"], result["evidence"]
    assert evidence["task_id"] == assessment["task_id"]
    assert evidence["artifact_type"] == "xlsx_snapshot_quality"
    source = assessment["source"]
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
    revision = declaration_body(declared, columns=[COLUMNS[0]])
    assert declare(client, headers, file_id, revision).status_code == 200
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
    response = request_quality(client, headers, file_id, digest)
    assert response.status_code == (503 if failure == "audit" else 409)
    assert list((tmp_path / "artifacts").glob("*/*.json")) == []
    assert list((tmp_path / "scratch").iterdir()) == []
