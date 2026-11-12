import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_metric_analysis import actor
from test_stage11_task_submission import credentials
from test_stage13_xlsx_preflight import (
    CONTENT_TYPES,
    WORKSHEET,
    package,
    relationships_xml,
    workbook_xml,
)
from test_stage13_xlsx_preview import (
    entries_for,
    formula_cell,
    number_cell,
    row,
    sheet,
    text_cell,
)

from generaldataagent.api.app import create_app
from generaldataagent.datasets import xlsx_intake
from generaldataagent.datasets.xlsx_intake import XlsxIntakeService
from generaldataagent.datasets.xlsx_intake_contracts import (
    ConfirmXlsxText,
    XlsxIntakeError,
    XlsxLimits,
)
from generaldataagent.datasets.xlsx_intake_store import (
    XlsxIntakeConflict,
    XlsxIntakeUnavailable,
    XlsxUploadStore,
)
from generaldataagent.observability import JsonlAuditSink
from generaldataagent.security.identity import SQLiteSessionBroker

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
RAW = package(
    entries_for(
        sheet(
            row(1, text_cell("A1", "name"), text_cell("B1", "amount")),
            row(2, text_cell("A2", "alpha"), number_cell("B2", "10")),
            row(3, text_cell("A3", "beta"), formula_cell("B3", "1+1")),
        )
    )
)


def build_service(
    tmp_path,
    *,
    limits=None,
    clock=None,
    scanner=None,
    retention=86400,
    max_files=20,
    max_total=200,
    audit=None,
):
    broker = SQLiteSessionBroker(tmp_path / "sessions.db", clock=clock)
    store = XlsxUploadStore(
        tmp_path / "xlsx.db",
        encryption_key=b"i" * 32,
        clock=clock,
        max_files_per_actor=max_files,
        max_total_files=max_total,
    )
    return XlsxIntakeService(
        store=store,
        broker=broker,
        audit=audit or JsonlAuditSink(tmp_path / "audit.jsonl"),
        limits=limits,
        scanner=scanner,
        retention_seconds=retention,
    )


def client_for(service, **kwargs):
    return TestClient(create_app(session_broker=service.broker, xlsx_intake=service, **kwargs))


def upload(client, headers, raw=RAW, *, sheet_name=None):
    path = "/v1/analysis/files/xlsx"
    if sheet_name is not None:
        path += f"?sheet={sheet_name}"
    return client.post(
        path,
        headers=headers | {"content-type": XLSX, "idempotency-key": "xlsx-1"},
        content=raw,
    )


def confirm_body(preview, **changes):
    body = {
        "schema_version": "1.0",
        "confirmed": True,
        "source_hash": preview["file"]["source_hash"],
        "preview_hash": preview["file"]["preview_hash"],
    }
    body.update(changes)
    return body


def confirm(client, headers, file_id, body):
    return client.post(
        f"/v1/analysis/files/xlsx/{file_id}/confirm",
        headers=headers | {"content-type": "application/json"},
        json=body,
    )


def test_api_stage_and_preview_keep_text_inert_and_source_private(tmp_path):
    service = build_service(tmp_path)
    token, headers = credentials(service.broker)
    client = client_for(service)
    response = upload(client, headers)
    assert response.status_code == 201, response.text
    preview = response.json()
    file_id = preview["file"]["file_id"]
    assert preview["file"]["source_hash"] == "sha256:" + hashlib.sha256(RAW).hexdigest()
    assert preview["file"]["byte_count"] == len(RAW)
    assert preview["file"]["status"] == "staged"
    assert preview["file"]["dlp_scope"] == "preview-text-only"
    assert preview["file"]["config"]["sheet"] == "Data"
    assert preview["preflight"]["inspector"] == "gda-xlsx-preflight-v1"
    assert preview["data"]["header"] == ["name", "amount"]
    assert preview["data"]["rows"] == [["alpha", "10"], ["beta", ""]]
    assert preview["data"]["formulas"][0]["formula"] == "1+1"
    assert preview["data"]["evaluation"] == "never"
    assert client.get(f"/v1/analysis/files/xlsx/{file_id}", headers=headers).json() == preview
    assert RAW not in service.store.path.read_bytes()
    audit = (tmp_path / "audit.jsonl").read_text()
    assert token not in audit and "alpha" not in audit and "name" not in audit


@pytest.mark.parametrize(
    ("content_type", "encoding", "status"),
    [
        ("application/vnd.ms-excel", "identity", 415),
        ("text/csv", "identity", 415),
        ("", "identity", 415),
        (XLSX, "gzip", 415),
    ],
)
def test_api_requires_exact_ooxml_media_type_and_identity_encoding(
    tmp_path, content_type, encoding, status
):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    client = client_for(service)
    headers = headers | {
        "content-type": content_type,
        "content-encoding": encoding,
        "idempotency-key": "xlsx-1",
    }
    assert (
        client.post("/v1/analysis/files/xlsx", headers=headers, content=RAW).status_code == status
    )


def test_api_rejects_missing_idempotency_invalid_bytes_and_macros(tmp_path):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    client = client_for(service)
    base = {key: value for key, value in headers.items() if key != "idempotency-key"} | {
        "content-type": XLSX
    }
    assert client.post("/v1/analysis/files/xlsx", headers=base, content=RAW).status_code == 422
    base["idempotency-key"] = "xlsx-1"
    assert (
        client.post("/v1/analysis/files/xlsx", headers=base, content=b"not-a-zip").status_code
        == 422
    )
    entries = entries_for(sheet(row(1, text_cell("A1", "name"))))
    macro = package(entries + [("xl/vbaProject.bin", b"<x/>")])
    assert client.post("/v1/analysis/files/xlsx", headers=base, content=macro).status_code == 422
    assert client.post("/v1/analysis/files/xlsx", headers=base, content=b"").status_code == 422


def two_sheet_package():
    entries = [
        ("[Content_Types].xml", CONTENT_TYPES),
        ("xl/workbook.xml", workbook_xml(("First", "rId1"), ("Second", "rId2")).decode()),
        (
            "xl/_rels/workbook.xml.rels",
            relationships_xml(
                [
                    ("rId1", WORKSHEET, "worksheets/sheet1.xml", ""),
                    ("rId2", WORKSHEET, "worksheets/sheet2.xml", ""),
                ]
            ).decode(),
        ),
        ("xl/worksheets/sheet1.xml", sheet(row(1, text_cell("A1", "first")))),
        ("xl/worksheets/sheet2.xml", sheet(row(1, text_cell("A1", "second")))),
    ]
    return package(entries)


def test_explicit_sheet_is_pinned_and_unknown_sheet_is_rejected(tmp_path):
    service = build_service(tmp_path)
    token, headers = credentials(service.broker)
    client = client_for(service)
    data = two_sheet_package()
    response = upload(client, headers, data, sheet_name="Second")
    assert response.status_code == 201, response.text
    preview = response.json()
    assert preview["file"]["config"]["sheet"] == "Second"
    assert preview["data"]["header"] == ["second"]
    file_id = preview["file"]["file_id"]
    assert upload(client, headers, data, sheet_name="Missing").status_code == 422
    path = f"/v1/analysis/files/xlsx/{file_id}"
    assert client.get(path, headers=headers).json()["data"]["sheet"] == "Second"
    # A later read uses the pinned sheet even if the caller asks for another one.
    assert client.get(path + "?sheet=First", headers=headers).json()["data"]["sheet"] == "Second"


@pytest.mark.parametrize(
    "change",
    [
        {"user_id": "other"},
        {"tenant_id": "other"},
        {"project_id": "other"},
        {"data_domains": ("finance",)},
        {"data_domains": ("commerce", "finance")},
    ],
)
def test_file_ids_are_creator_tenant_project_and_exact_domain_private(tmp_path, change):
    service = build_service(tmp_path)
    token, _ = credentials(service.broker)
    preview = service.stage(RAW, actor(), token, "private")
    other = actor().model_copy(update=change)
    other_token, _ = credentials(service.broker, other)
    with pytest.raises(PermissionError):
        service.preview(preview.file.file_id, other, other_token)


def test_idempotent_replay_returns_same_file_and_changed_request_conflicts(tmp_path):
    service = build_service(tmp_path)
    token, _ = credentials(service.broker)
    first = service.stage(RAW, actor(), token, "same")
    replay = service.stage(RAW, actor(), token, "same")
    assert replay.file.file_id == first.file.file_id
    changed = package(entries_for(sheet(row(1, text_cell("A1", "different")))))
    with pytest.raises(XlsxIntakeConflict, match="request_changed"):
        service.stage(changed, actor(), token, "same")
    with pytest.raises(XlsxIntakeConflict, match="idempotency_key_invalid"):
        service.stage(RAW, actor(), token, "bad key")


def test_expiry_and_capacity_include_unusable_retained_rows(tmp_path):
    now = [datetime.now(UTC)]
    service = build_service(tmp_path, clock=lambda: now[0], retention=2, max_files=1, max_total=1)
    token, _ = credentials(service.broker)
    preview = service.stage(RAW, actor(), token, "ttl")
    now[0] += timedelta(seconds=3)
    with pytest.raises(PermissionError):
        service.preview(preview.file.file_id, actor(), token)
    with pytest.raises(PermissionError):
        service.stage(RAW, actor(), token, "ttl")
    changed = package(entries_for(sheet(row(1, text_cell("A1", "other")))))
    with pytest.raises(XlsxIntakeConflict, match="capacity"):
        service.stage(changed, actor(), token, "new")


@pytest.mark.parametrize("column", ["source", "body", "request_hash", "user_version"])
def test_tampered_source_metadata_or_schema_never_reexecutes(tmp_path, column):
    service = build_service(tmp_path)
    token, _ = credentials(service.broker)
    preview = service.stage(RAW, actor(), token, "tamper")
    with sqlite3.connect(service.store.path) as db:
        if column == "source":
            db.execute("UPDATE xlsx_uploads SET source=X'00'")
        elif column == "body":
            db.execute("UPDATE xlsx_uploads SET body=X'00'")
        elif column == "request_hash":
            db.execute("UPDATE xlsx_uploads SET request_hash='0'")
        else:
            db.execute("PRAGMA user_version=2")
    with pytest.raises((PermissionError, XlsxIntakeUnavailable)):
        service.preview(preview.file.file_id, actor(), token)


def test_wrong_key_or_non_dedicated_database_is_unavailable(tmp_path):
    service = build_service(tmp_path)
    token, _ = credentials(service.broker)
    service.stage(RAW, actor(), token, "key")
    with pytest.raises(PermissionError):
        XlsxUploadStore(service.store.path, encryption_key=b"x" * 32)
    unrelated = tmp_path / "unrelated.db"
    with sqlite3.connect(unrelated) as db:
        db.execute("CREATE TABLE other (id INTEGER PRIMARY KEY)")
    with pytest.raises(ValueError, match="dedicated_xlsx_database_required"):
        XlsxUploadStore(unrelated, encryption_key=b"i" * 32)


def test_parser_and_processing_drift_require_restage(tmp_path, monkeypatch):
    service = build_service(tmp_path)
    token, _ = credentials(service.broker)
    preview = service.stage(RAW, actor(), token, "drift")
    monkeypatch.setattr(xlsx_intake, "parser_fingerprint", lambda *args: "sha256:" + "1" * 64)
    with pytest.raises(XlsxIntakeConflict, match="restage_required"):
        service.preview(preview.file.file_id, actor(), token)
    monkeypatch.undo()
    monkeypatch.setattr(xlsx_intake, "_PROCESSING_FINGERPRINT", "sha256:" + "2" * 64)
    with pytest.raises(XlsxIntakeConflict, match="restage_required"):
        service.preview(preview.file.file_id, actor(), token)


def test_changed_preview_result_is_denied(tmp_path, monkeypatch):
    service = build_service(tmp_path)
    token, _ = credentials(service.broker)
    preview = service.stage(RAW, actor(), token, "preview-drift")
    original = xlsx_intake.preview_xlsx

    def drifted(data, *, sheet=None):
        result = original(data, sheet=sheet)
        return result.model_copy(update={"data": result.data.model_copy(update={"sheet": "Other"})})

    monkeypatch.setattr(xlsx_intake, "preview_xlsx", drifted)
    with pytest.raises(PermissionError, match="preview_binding"):
        service.preview(preview.file.file_id, actor(), token)


def test_audit_or_dlp_failure_leaves_no_staged_row(tmp_path):
    class FailingAudit:
        def emit(self, event):
            raise RuntimeError("audit down")

    failing = build_service(tmp_path / "audit", audit=FailingAudit())
    token, _ = credentials(failing.broker)
    with pytest.raises(XlsxIntakeUnavailable, match="audit_unavailable"):
        failing.stage(RAW, actor(), token, "audit")
    with sqlite3.connect(failing.store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM xlsx_uploads").fetchone()[0] == 0

    class DenyScanner:
        def check(self, content):
            raise PermissionError("dlp_denied")

    denied = build_service(tmp_path / "dlp", scanner=DenyScanner())
    token, _ = credentials(denied.broker)
    with pytest.raises(PermissionError, match="dlp_denied"):
        denied.stage(RAW, actor(), token, "dlp")
    with sqlite3.connect(denied.store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM xlsx_uploads").fetchone()[0] == 0


def test_credential_header_is_denied_before_storage(tmp_path):
    service = build_service(tmp_path)
    token, _ = credentials(service.broker)
    secret = package(entries_for(sheet(row(1, text_cell("A1", "password")))))
    with pytest.raises(PermissionError, match="credential_column_denied"):
        service.stage(secret, actor(), token, "secret")


def test_limits_reject_oversize_and_non_bytes_before_any_row(tmp_path):
    service = build_service(tmp_path, limits=XlsxLimits(max_bytes=len(RAW) - 1))
    token, _ = credentials(service.broker)
    with pytest.raises(ValueError, match="request_byte_limit"):
        service.stage(RAW, actor(), token, "too-large")
    with pytest.raises(XlsxIntakeError, match="input_empty"):
        service.stage("text", actor(), token, "not-bytes")
    with sqlite3.connect(service.store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM xlsx_uploads").fetchone()[0] == 0


def test_api_oversize_is_413_and_default_off_and_production_gates(tmp_path):
    assert TestClient(create_app()).post("/v1/analysis/files/xlsx", content=RAW).status_code == 503
    service = build_service(tmp_path, limits=XlsxLimits(max_bytes=len(RAW) - 1))
    with pytest.raises(ValueError, match="must_share_identity"):
        create_app(xlsx_intake=service)
    with pytest.raises(ValueError, match="not_qualified_for_production"):
        create_app(xlsx_intake=service, session_broker=service.broker, production_mode=True)
    _, headers = credentials(service.broker)
    client = client_for(service)
    assert upload(client, headers).status_code == 413
    client = client_for(build_service(tmp_path / "off"), service_enabled=lambda: False)
    assert upload(client, headers).status_code == 503


def test_explicit_confirmation_is_pinned_reopenable_and_idempotent(tmp_path):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    client = client_for(service)
    preview = upload(client, headers).json()
    file_id = preview["file"]["file_id"]
    body = confirm_body(preview)
    first = confirm(client, headers, file_id, body)
    assert first.status_code == 200, first.text
    confirmed = first.json()
    assert confirmed["file"]["status"] == "confirmed"
    assert confirmed["file"]["confirmation_hash"].startswith("sha256:")
    assert confirmed["file"]["confirmed_at"] >= confirmed["file"]["created_at"]
    assert confirmed["data"] == preview["data"]
    assert confirmed["preflight"] == preview["preflight"]
    assert confirm(client, headers, file_id, body).json() == confirmed
    assert client.get(f"/v1/analysis/files/xlsx/{file_id}", headers=headers).json() == confirmed
    assert (tmp_path / "audit.jsonl").read_text().count("xlsx.confirm_intent") == 1


def test_confirmation_requires_json_media_type_and_explicit_true(tmp_path):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    client = client_for(service)
    preview = upload(client, headers).json()
    path = f"/v1/analysis/files/xlsx/{preview['file']['file_id']}/confirm"
    assert (
        client.post(
            path,
            headers=headers | {"content-type": "text/plain"},
            content=b'{"confirmed": true}',
        ).status_code
        == 415
    )
    assert (
        client.post(path, headers=headers, json=confirm_body(preview, confirmed=False)).status_code
        == 422
    )
    assert (
        client.post(path, headers=headers, json=confirm_body(preview, confirmed=1)).status_code
        == 422
    )
    assert (
        client.post(path, headers=headers, json=confirm_body(preview, extra="x")).status_code == 422
    )
    assert (
        client.post(
            path, headers=headers | {"content-type": "application/json"}, content=b""
        ).status_code
        == 422
    )
    with pytest.raises(ValueError, match="explicit_text_confirmation"):
        ConfirmXlsxText(
            confirmed=False,
            source_hash=preview["file"]["source_hash"],
            preview_hash=preview["file"]["preview_hash"],
        )


def test_confirmation_rejects_changed_hashes_and_leaves_status_staged(tmp_path):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    client = client_for(service)
    preview = upload(client, headers).json()
    file_id = preview["file"]["file_id"]
    other = "sha256:" + "0" * 64
    assert (
        confirm(client, headers, file_id, confirm_body(preview, source_hash=other)).status_code
        == 409
    )
    assert (
        confirm(client, headers, file_id, confirm_body(preview, preview_hash=other)).status_code
        == 409
    )
    assert (
        client.get(f"/v1/analysis/files/xlsx/{file_id}", headers=headers).json()["file"]["status"]
        == "staged"
    )


@pytest.mark.parametrize(
    "change", [{"user_id": "other"}, {"tenant_id": "other"}, {"project_id": "other"}]
)
def test_confirmation_is_creator_scoped_and_expiry_bound(tmp_path, change):
    now = [datetime.now(UTC)]
    service = build_service(tmp_path, clock=lambda: now[0], retention=2)
    token, _ = credentials(service.broker)
    preview = service.stage(RAW, actor(), token, "confirm")
    request = ConfirmXlsxText(
        confirmed=True,
        source_hash=preview.file.source_hash,
        preview_hash=preview.file.preview_hash,
    )
    other = actor().model_copy(update=change)
    other_token, _ = credentials(service.broker, other)
    with pytest.raises(PermissionError):
        service.confirm(preview.file.file_id, other, other_token, request)
    now[0] += timedelta(seconds=3)
    with pytest.raises(PermissionError):
        service.confirm(preview.file.file_id, actor(), token, request)


def test_confirmation_revalidates_parser_and_preview_before_persisting(tmp_path, monkeypatch):
    service = build_service(tmp_path)
    token, _ = credentials(service.broker)
    preview = service.stage(RAW, actor(), token, "drift")
    request = ConfirmXlsxText(
        confirmed=True,
        source_hash=preview.file.source_hash,
        preview_hash=preview.file.preview_hash,
    )
    monkeypatch.setattr(xlsx_intake, "parser_fingerprint", lambda *args: "sha256:" + "1" * 64)
    with pytest.raises(XlsxIntakeConflict, match="restage_required"):
        service.confirm(preview.file.file_id, actor(), token, request)
    monkeypatch.undo()
    original = xlsx_intake.preview_xlsx

    def drifted(data, *, sheet=None):
        result = original(data, sheet=sheet)
        return result.model_copy(update={"data": result.data.model_copy(update={"sheet": "Other"})})

    monkeypatch.setattr(xlsx_intake, "preview_xlsx", drifted)
    with pytest.raises(PermissionError, match="preview_binding"):
        service.confirm(preview.file.file_id, actor(), token, request)


def test_confirmation_tampering_and_audit_failure_never_confirm(tmp_path):
    service = build_service(tmp_path)
    token, _ = credentials(service.broker)
    preview = service.stage(RAW, actor(), token, "tamper-confirm")
    request = ConfirmXlsxText(
        confirmed=True,
        source_hash=preview.file.source_hash,
        preview_hash=preview.file.preview_hash,
    )
    with sqlite3.connect(service.store.path) as db:
        db.execute("UPDATE xlsx_uploads SET body=X'00'")
    with pytest.raises(PermissionError):
        service.confirm(preview.file.file_id, actor(), token, request)

    class FlakyAudit:
        calls = 0

        def emit(self, event):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("audit down")

    flaky = build_service(tmp_path / "flaky", audit=FlakyAudit())
    flaky_token, _ = credentials(flaky.broker)
    staged = flaky.stage(RAW, actor(), flaky_token, "audit-confirm")
    with pytest.raises(XlsxIntakeUnavailable, match="audit_unavailable"):
        flaky.confirm(
            staged.file.file_id,
            actor(),
            flaky_token,
            ConfirmXlsxText(
                confirmed=True,
                source_hash=staged.file.source_hash,
                preview_hash=staged.file.preview_hash,
            ),
        )
    assert flaky.preview(staged.file.file_id, actor(), flaky_token).file.status == "staged"


def test_replaying_stage_after_confirmation_keeps_confirmed_status(tmp_path):
    service = build_service(tmp_path)
    token, _ = credentials(service.broker)
    preview = service.stage(RAW, actor(), token, "same")
    confirmed = service.confirm(
        preview.file.file_id,
        actor(),
        token,
        ConfirmXlsxText(
            confirmed=True,
            source_hash=preview.file.source_hash,
            preview_hash=preview.file.preview_hash,
        ),
    )
    assert confirmed.file.status == "confirmed"
    replay = service.stage(RAW, actor(), token, "same")
    assert replay.file.status == "confirmed"
    assert replay.file.confirmation_hash == confirmed.file.confirmation_hash


def test_confirmation_body_is_bounded(tmp_path):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    client = client_for(service)
    preview = upload(client, headers).json()
    path = f"/v1/analysis/files/xlsx/{preview['file']['file_id']}/confirm"
    oversized = b"{" + b" " * 1024
    assert (
        client.post(
            path,
            headers=headers | {"content-type": "application/json"},
            content=oversized,
        ).status_code
        == 413
    )
