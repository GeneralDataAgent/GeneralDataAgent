import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from test_metric_analysis import actor
from test_stage11_task_submission import credentials
from test_stage13_xlsx_conformance import COLUMNS, prepared
from test_stage13_xlsx_intake import (
    RAW,
    XLSX,
    build_service,
    client_for,
    declaration_body,
    declare,
)

from generaldataagent.datasets import xlsx_intake
from generaldataagent.datasets.xlsx_declarations import (
    XlsxDeclarationPage,
    XlsxDeclarationSummary,
    XlsxDeclaredColumn,
)


def declarations(client, headers):
    return client.get("/v1/analysis/files/xlsx/declarations", headers=headers)


def snapshot(client, headers, file_id):
    return client.post(f"/v1/analysis/files/xlsx/{file_id}/snapshot", headers=headers)


def test_listing_returns_only_declared_metadata(tmp_path):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    client = client_for(service)
    assert declarations(client, headers).json() == {
        "schema_version": "1.0",
        "items": [],
        "declarations": "metadata-only",
        "headers": "not-returned",
        "tokens": "counts-only",
        "values": "not-returned",
    }
    staged = client.post(
        "/v1/analysis/files/xlsx",
        headers=headers | {"content-type": XLSX, "idempotency-key": "decl-undeclared"},
        content=RAW,
    )
    assert staged.status_code == 201, staged.text
    assert declarations(client, headers).json()["items"] == []
    file_id, declared = prepared(client, headers)
    page = declarations(client, headers).json()
    assert len(page["items"]) == 1
    item = page["items"][0]
    assert item["file_id"] == file_id
    assert item["task_id"] == declared["file"]["task_id"]
    assert item["status"] == "confirmed"
    assert item["availability"] == "compatible"
    assert item["sheet"] == "Data"
    assert item["header_row"] == 1
    assert item["duplicate_rows"] == "reject"
    assert item["interpretation_hash"] == declared["interpretation"]["interpretation_hash"]
    assert item["snapshot_state"] == "none"
    assert item["created_at"] == declared["file"]["created_at"]
    assert item["expires_at"] == declared["file"]["expires_at"]
    assert item["columns"] == [
        {
            "schema_version": "1.0",
            "index": 0,
            "type": "text",
            "empty_is_missing": True,
            "missing_token_count": 1,
        },
        {
            "schema_version": "1.0",
            "index": 1,
            "type": "integer",
            "empty_is_missing": True,
            "missing_token_count": 0,
        },
        {
            "schema_version": "1.0",
            "index": 2,
            "type": "boolean",
            "empty_is_missing": True,
            "missing_token_count": 0,
        },
    ]
    body = json.dumps(page)
    for secret in ("name", "amount", "flag", "alpha", "beta", "NA", "Data!"):
        assert secret not in body
    events = [
        json.loads(line)
        for line in (tmp_path / "audit.jsonl").read_text().splitlines()
        if json.loads(line)["event_type"] == "xlsx.declarations_listed"
    ]
    assert events[-1]["payload"] == {"item_count": 1}
    assert "name" not in json.dumps(events[-1])


def test_listing_reports_snapshot_state_against_the_current_declaration(tmp_path):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    client = client_for(service)
    file_id, first = prepared(client, headers)
    assert snapshot(client, headers, file_id).status_code == 200
    assert declarations(client, headers).json()["items"][0]["snapshot_state"] == "current"
    confirmed = client.get(f"/v1/analysis/files/xlsx/{file_id}", headers=headers).json()
    redeclared = declare(
        client,
        headers,
        file_id,
        declaration_body(confirmed, columns=COLUMNS[:1]),
    )
    assert redeclared.status_code == 200, redeclared.text
    page = declarations(client, headers).json()
    assert page["items"][0]["snapshot_state"] == "stale"
    assert (
        page["items"][0]["interpretation_hash"]
        == redeclared.json()["interpretation"]["interpretation_hash"]
        != first["interpretation"]["interpretation_hash"]
    )


def test_listing_is_creator_scoped_and_skips_expired_rows(tmp_path):
    now = [datetime.now(UTC)]
    service = build_service(tmp_path, clock=lambda: now[0], retention=60)
    _, headers = credentials(service.broker)
    client = client_for(service)
    file_id, _ = prepared(client, headers)
    assert len(declarations(client, headers).json()["items"]) == 1
    other = actor().model_copy(update={"user_id": "other"})
    _, other_headers = credentials(service.broker, other)
    assert declarations(client, other_headers).json()["items"] == []
    now[0] += timedelta(seconds=61)
    assert declarations(client, headers).json()["items"] == []


def test_listing_marks_release_drift_without_returning_values(tmp_path, monkeypatch):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    client = client_for(service)
    prepared(client, headers)
    monkeypatch.setattr(xlsx_intake, "parser_fingerprint", lambda *args: "sha256:" + "1" * 64)
    page = declarations(client, headers).json()
    assert page["items"][0]["availability"] == "release_incompatible"
    assert "alpha" not in json.dumps(page)


def test_listing_returns_no_page_when_audit_fails(tmp_path):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    client = client_for(service)
    prepared(client, headers)

    def fail(event):
        raise RuntimeError("audit down")

    service.audit.emit = fail
    response = declarations(client, headers)
    assert response.status_code == 503
    assert response.json()["detail"] == "xlsx_intake_unavailable"


def test_listing_refuses_a_page_if_a_file_expires_during_audit(tmp_path, monkeypatch):
    now = [datetime(2026, 11, 13, tzinfo=UTC)]
    service = build_service(tmp_path, clock=lambda: now[0], retention=60)
    _, headers = credentials(service.broker)
    client = client_for(service)
    prepared(client, headers)
    emit = service.audit.emit

    def advance_clock(event):
        emit(event)
        now[0] += timedelta(seconds=60)

    with monkeypatch.context() as patch:
        patch.setattr(service.audit, "emit", advance_clock)
        response = declarations(client, headers)
    assert response.status_code == 503
    assert response.json() == {"detail": "xlsx_intake_unavailable"}
    assert declarations(client, headers).json()["items"] == []


def test_listing_fails_closed_on_capacity_and_corruption(tmp_path):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    client = client_for(service)
    prepared(client, headers)
    store = service.store
    with sqlite3.connect(store.path) as db:
        scope = store.scope(actor())
        for index in range(21):
            db.execute(
                "INSERT INTO xlsx_uploads VALUES (?,?,?,?,?,?)",
                (f"bogus-{index}", scope, f"idem-{index}", "x", b"x", b"x"),
            )
    response = declarations(client, headers)
    assert response.status_code == 503
    assert response.json()["detail"] == "xlsx_intake_unavailable"
    with sqlite3.connect(store.path) as db:
        db.execute("DELETE FROM xlsx_uploads WHERE file_id LIKE 'bogus-%'")
        db.execute("UPDATE xlsx_uploads SET body=? WHERE file_id NOT LIKE 'bogus-%'", (b"corrupt",))
    response = declarations(client, headers)
    assert response.status_code == 404
    assert response.json()["detail"] == "xlsx_file_unavailable"


def test_listing_route_precedes_the_file_id_route(tmp_path):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    response = client_for(service).get("/v1/analysis/files/xlsx/declarations", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["declarations"] == "metadata-only"


def test_declaration_contracts_hold_invariants():
    assert XlsxDeclaredColumn(index=0, type="text", empty_is_missing=True, missing_token_count=0)
    with pytest.raises(ValidationError, match="xlsx_declaration_column_order_invalid"):
        XlsxDeclarationSummary(
            file_id="00000000-0000-0000-0000-000000000001",
            task_id="00000000-0000-0000-0000-000000000002",
            availability="compatible",
            sheet="Data",
            header_row=1,
            columns=(
                XlsxDeclaredColumn(
                    index=1, type="text", empty_is_missing=True, missing_token_count=0
                ),
                XlsxDeclaredColumn(
                    index=0, type="text", empty_is_missing=True, missing_token_count=0
                ),
            ),
            duplicate_rows="reject",
            interpretation_hash="sha256:" + "0" * 64,
            snapshot_state="none",
            created_at="2026-11-13T00:00:00Z",
            expires_at="2026-11-13T00:00:00Z",
        )
    summary = XlsxDeclarationSummary(
        file_id="00000000-0000-0000-0000-000000000001",
        task_id="00000000-0000-0000-0000-000000000002",
        availability="compatible",
        sheet="Data",
        header_row=1,
        columns=(),
        duplicate_rows="keep-all",
        interpretation_hash="sha256:" + "0" * 64,
        snapshot_state="current",
        created_at="2026-11-13T00:00:00Z",
        expires_at="2026-11-13T01:00:00Z",
    )
    with pytest.raises(ValidationError, match="xlsx_declaration_duplicate"):
        XlsxDeclarationPage(items=(summary, summary))
    assert XlsxDeclarationPage(items=(summary,)).headers == "not-returned"
    assert XlsxDeclarationPage(items=()).tokens == "counts-only"
