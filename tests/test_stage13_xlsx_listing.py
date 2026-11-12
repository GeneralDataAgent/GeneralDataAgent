import json
from datetime import UTC, datetime, timedelta

import pytest
from test_metric_analysis import actor
from test_stage11_task_submission import credentials
from test_stage13_xlsx_intake import (
    RAW,
    XLSX,
    build_service,
    client_for,
    confirm,
    confirm_body,
    declaration_body,
    declare,
)

from generaldataagent.datasets import xlsx_intake


def stage(client, headers, *, key, source=RAW):
    return client.post(
        "/v1/analysis/files/xlsx",
        headers=headers | {"content-type": XLSX, "idempotency-key": key},
        content=source,
    )


def listed(client, headers, **params):
    return client.get("/v1/analysis/files/xlsx", headers=headers, params=params)


def test_listing_is_empty_then_metadata_only(tmp_path):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    client = client_for(service)
    initial = listed(client, headers)
    assert initial.status_code == 200, initial.text
    assert initial.json() == {"schema_version": "1.0", "items": [], "next_cursor": None}
    staged = stage(client, headers, key="list-1")
    assert staged.status_code == 201, staged.text
    preview = staged.json()
    page = listed(client, headers).json()
    assert len(page["items"]) == 1
    item = page["items"][0]
    assert item["file_id"] == preview["file"]["file_id"]
    assert item["task_id"] == preview["file"]["task_id"]
    assert item["status"] == "staged"
    assert item["availability"] == "compatible"
    assert item["sheet"] == "Data"
    assert item["row_count"] == preview["data"]["row_count"]
    assert item["column_count"] == preview["data"]["column_count"]
    assert item["byte_count"] == len(RAW)
    assert item["has_interpretation"] is False
    assert item["created_at"] == preview["file"]["created_at"]
    assert item["expires_at"] == preview["file"]["expires_at"]
    body = json.dumps(page)
    for secret in ("alpha", "beta", "name", "amount", "sha256:", "list-1", "formula"):
        assert secret not in body
    confirmed = confirm(client, headers, item["file_id"], confirm_body(preview)).json()
    assert listed(client, headers).json()["items"][0]["status"] == "confirmed"
    declared = declare(
        client,
        headers,
        item["file_id"],
        declaration_body(confirmed, columns=[{"index": 0, "header": "name", "type": "text"}]),
    )
    assert declared.status_code == 200, declared.text
    assert listed(client, headers).json()["items"][0]["has_interpretation"] is True
    audit = (tmp_path / "audit.jsonl").read_text()
    assert "xlsx.files_listed" in audit
    listed_events = [
        json.loads(line)
        for line in audit.splitlines()
        if json.loads(line)["event_type"] == "xlsx.files_listed"
    ]
    assert listed_events[-1]["payload"] == {"item_count": 1, "has_more": False}
    assert "alpha" not in json.dumps(listed_events[-1])


def test_listing_pages_with_a_scope_bound_cursor(tmp_path):
    now = [datetime.now(UTC)]
    service = build_service(tmp_path, clock=lambda: now[0])
    _, headers = credentials(service.broker)
    client = client_for(service)
    ids = []
    for index in range(3):
        response = stage(client, headers, key=f"page-{index}")
        assert response.status_code == 201, response.text
        ids.append(response.json()["file"]["file_id"])
        now[0] += timedelta(seconds=1)
    first = listed(client, headers, limit=2).json()
    assert len(first["items"]) == 2
    assert first["next_cursor"]
    second = listed(client, headers, limit=2, cursor=first["next_cursor"]).json()
    assert len(second["items"]) == 1
    assert second["next_cursor"] is None
    assert [item["file_id"] for item in first["items"] + second["items"]] == ids
    cursor = first["next_cursor"]
    assert listed(client, headers, limit=3, cursor=cursor).status_code == 409
    tampered = ("A" if cursor[0] != "A" else "B") + cursor[1:]
    assert listed(client, headers, limit=2, cursor=tampered).status_code == 409
    assert listed(client, headers, limit=2, cursor="not-a-cursor").status_code == 409
    other = actor().model_copy(update={"user_id": "other"})
    _, other_headers = credentials(service.broker, other)
    assert listed(client, other_headers, limit=2, cursor=cursor).status_code == 409
    now[0] += timedelta(minutes=6)
    assert listed(client, headers, limit=2, cursor=cursor).status_code == 409


def test_listing_skips_expired_rows_and_is_creator_scoped(tmp_path):
    now = [datetime.now(UTC)]
    service = build_service(tmp_path, clock=lambda: now[0], retention=2)
    _, headers = credentials(service.broker)
    client = client_for(service)
    for index in range(2):
        assert stage(client, headers, key=f"scope-{index}").status_code == 201
    assert len(listed(client, headers).json()["items"]) == 2
    other = actor().model_copy(update={"user_id": "other"})
    _, other_headers = credentials(service.broker, other)
    assert listed(client, other_headers).json()["items"] == []
    now[0] += timedelta(seconds=3)
    assert listed(client, headers).json()["items"] == []
    assert listed(client, headers).json()["next_cursor"] is None


def test_listing_reports_release_drift_without_reinterpreting(tmp_path):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    client = client_for(service)
    preview = stage(client, headers, key="drift-list").json()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(xlsx_intake, "parser_fingerprint", lambda *args: "sha256:" + "1" * 64)
    try:
        item = listed(client, headers).json()["items"][0]
        assert item["availability"] == "release_incompatible"
        assert item["status"] == "staged"
        assert (
            client.get(
                f"/v1/analysis/files/xlsx/{preview['file']['file_id']}", headers=headers
            ).status_code
            == 409
        )
    finally:
        monkeypatch.undo()


def test_listing_returns_no_page_when_audit_fails(tmp_path):
    class FlakyAudit:
        fail = False

        def emit(self, event):
            if self.fail:
                raise RuntimeError("audit down")

    audit = FlakyAudit()
    service = build_service(tmp_path, audit=audit)
    _, headers = credentials(service.broker)
    client = client_for(service)
    assert stage(client, headers, key="audit-list").status_code == 201
    audit.fail = True
    response = listed(client, headers)
    assert response.status_code == 503
    assert response.json()["detail"] == "xlsx_intake_unavailable"


def test_listing_rejects_out_of_range_page_sizes(tmp_path):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    client = client_for(service)
    assert listed(client, headers, limit=0).status_code == 422
    assert listed(client, headers, limit=21).status_code == 422
    assert listed(client, headers, cursor="x" * 4097).status_code == 422
