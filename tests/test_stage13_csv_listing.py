import json
from datetime import UTC, datetime, timedelta

import pytest
from test_metric_analysis import actor
from test_stage11_task_submission import credentials
from test_stage13_csv_intake import RAW, build_service, client_for, confirmation

from generaldataagent.data_engineering import DataEngineeringRuntime
from generaldataagent.datasets import csv_intake, csv_parser
from generaldataagent.datasets.csv_intake_store import CsvIntakeConflict, CsvIntakeUnavailable
from generaldataagent.datasets.csv_listing import CsvFileListing

URL = "/v1/analysis/files/csv"


def stage_many(service, token, count=3):
    return [service.stage(RAW, actor(), token, f"file-{i}") for i in range(count)]


def test_api_metadata_discovery_reopens_state_without_reupload_or_values(tmp_path):
    service = build_service(tmp_path)
    token, headers = credentials(service.broker)
    previews = stage_many(service, token, 2)
    service.confirm(previews[1].file.file_id, actor(), token, confirmation(previews[1]))
    rebuilt = build_service(tmp_path)
    _, fresh_headers = credentials(rebuilt.broker)
    response = client_for(rebuilt).get(URL, headers=fresh_headers)
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["schema_version"] == "1.0" and page["next_cursor"] is None
    assert [item["file_id"] for item in page["items"]] == [str(p.file.file_id) for p in previews]
    assert [item["status"] for item in page["items"]] == ["staged", "profiled"]
    assert all(item["availability"] == "compatible" for item in page["items"])
    assert set(page["items"][0]) == {
        "schema_version",
        "file_id",
        "task_id",
        "status",
        "availability",
        "created_at",
        "expires_at",
    }
    assert all(value not in response.text for value in ["amount", "=SUM", '"001"', "sample_rows"])
    selected = page["items"][1]["file_id"]
    assert (
        client_for(rebuilt).get(f"{URL}/{selected}/profile", headers=fresh_headers).status_code
        == 200
    )
    audit = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    listed = [e for e in audit if e["event_type"] == "csv.files_listed"]
    assert listed[-1]["payload"] == {"item_count": 2, "has_more": False}
    assert headers["authorization"] not in json.dumps(audit)


def test_snapshot_order_ties_new_upload_and_live_status_no_parse_or_profile(tmp_path, monkeypatch):
    now = datetime(2026, 11, 3, tzinfo=UTC)
    service = build_service(tmp_path, clock=lambda: now)
    token, _ = credentials(service.broker)
    previews = stage_many(service, token, 4)
    ordered = sorted(previews, key=lambda p: str(p.file.file_id))
    first = CsvFileListing(service).list_files(actor(), token, limit=1)
    assert first.items[0].file_id == ordered[0].file.file_id
    service.stage(b"other\nvalue\n", actor(), token, "inserted-after-snapshot")
    service.confirm(ordered[1].file.file_id, actor(), token, confirmation(ordered[1]))
    rebuilt = build_service(tmp_path, clock=lambda: now)
    fresh_token, _ = credentials(rebuilt.broker)

    def no_execution(*args, **kwargs):
        raise AssertionError("metadata listing must not parse or execute profiling")

    monkeypatch.setattr(csv_parser, "parse_csv", no_execution)
    monkeypatch.setattr(DataEngineeringRuntime, "profile_relation", no_execution)
    items, cursor = list(first.items), first.next_cursor
    while cursor:
        page = CsvFileListing(rebuilt).list_files(actor(), fresh_token, limit=1, cursor=cursor)
        items.extend(page.items)
        cursor = page.next_cursor
    assert [i.file_id for i in items] == [p.file.file_id for p in ordered]
    assert items[1].status == "profiled"
    assert len(CsvFileListing(rebuilt).list_files(actor(), fresh_token).items) == 5


@pytest.mark.parametrize(
    "field,value",
    [
        ("user_id", "other"),
        ("tenant_id", "other"),
        ("project_id", "other"),
        ("data_domains", ("other",)),
        ("data_domains", ("commerce", "other")),
    ],
)
def test_creator_exact_domain_inventory_and_cursor_scope(tmp_path, field, value):
    service = build_service(tmp_path)
    token, _ = credentials(service.broker)
    stage_many(service, token)
    cursor = CsvFileListing(service).list_files(actor(), token, limit=1).next_cursor
    other = actor().model_copy(update={field: value})
    other_token, _ = credentials(service.broker, other)
    assert CsvFileListing(service).list_files(other, other_token).items == ()
    with pytest.raises(CsvIntakeConflict):
        CsvFileListing(service).list_files(other, other_token, limit=1, cursor=cursor)
    with pytest.raises(PermissionError):
        CsvFileListing(service).list_files(other, token)


@pytest.mark.parametrize(
    "mutation", ["bitflip", "truncated", "plaintext", "oversize", "padding", "empty"]
)
def test_malformed_or_tampered_cursor_returns_fixed_conflict(tmp_path, mutation):
    service = build_service(tmp_path)
    token, headers = credentials(service.broker)
    stage_many(service, token)
    cursor = CsvFileListing(service).list_files(actor(), token, limit=1).next_cursor
    bad = {
        "bitflip": ("A" if cursor[0] != "A" else "B") + cursor[1:],
        "truncated": cursor[:-15],
        "plaintext": '{"scope":"user-1"}',
        "oversize": "a" * 4097,
        "padding": cursor + "=",
        "empty": "",
    }[mutation]
    response = client_for(service).get(URL, headers=headers, params={"limit": 1, "cursor": bad})
    assert response.status_code == (422 if mutation in {"oversize", "empty"} else 409)
    assert "user-1" not in response.text and "decrypt" not in response.text
    assert len(CsvFileListing(service).list_files(actor(), token).items) == 3


def test_cursor_page_size_binding_and_fixed_expiry_without_refresh_extension(tmp_path):
    now = [datetime(2026, 11, 3, tzinfo=UTC)]
    service = build_service(tmp_path, clock=lambda: now[0])
    token, _ = credentials(service.broker)
    stage_many(service, token, 4)
    listing = CsvFileListing(service)
    first = listing.list_files(actor(), token, limit=1)
    with pytest.raises(CsvIntakeConflict):
        listing.list_files(actor(), token, limit=2, cursor=first.next_cursor)
    now[0] += timedelta(seconds=299)
    second = listing.list_files(actor(), token, limit=1, cursor=first.next_cursor)
    now[0] += timedelta(seconds=1)
    with pytest.raises(CsvIntakeConflict):
        listing.list_files(actor(), token, limit=1, cursor=second.next_cursor)
    assert len(listing.list_files(actor(), token).items) == 4


def test_file_expiry_omits_remaining_without_releasing_retained_quota(tmp_path):
    now = [datetime(2026, 11, 3, tzinfo=UTC)]
    service = build_service(tmp_path, clock=lambda: now[0], retention=2, max_files=2)
    token, _ = credentials(service.broker)
    stage_many(service, token, 2)
    listing = CsvFileListing(service)
    first = listing.list_files(actor(), token, limit=1)
    now[0] += timedelta(seconds=2)
    page = listing.list_files(actor(), token, limit=1, cursor=first.next_cursor)
    assert page.items == () and page.next_cursor is None
    assert listing.list_files(actor(), token).items == ()
    with pytest.raises(CsvIntakeConflict):
        service.stage(RAW, actor(), token, "capacity-still-retained")


@pytest.mark.parametrize("changed", ["parser", "processing"])
def test_release_incompatible_records_discoverable_but_cursor_rejected(
    tmp_path, monkeypatch, changed
):
    service = build_service(tmp_path)
    token, _ = credentials(service.broker)
    previews = stage_many(service, token, 2)
    service.confirm(previews[0].file.file_id, actor(), token, confirmation(previews[0]))
    cursor = CsvFileListing(service).list_files(actor(), token, limit=1).next_cursor
    if changed == "parser":
        monkeypatch.setattr(csv_parser, "parser_fingerprint", lambda _: "sha256:" + "0" * 64)
    else:
        monkeypatch.setattr(csv_intake, "_PROFILE_FINGERPRINT", "sha256:" + "0" * 64)
    page = CsvFileListing(service).list_files(actor(), token)
    assert [i.availability for i in page.items] == ["release_incompatible"] * 2
    assert [i.status for i in page.items] == ["profiled", "staged"]
    with pytest.raises(CsvIntakeConflict):
        CsvFileListing(service).list_files(actor(), token, limit=1, cursor=cursor)
    with pytest.raises(CsvIntakeConflict):
        service.preview(previews[0].file.file_id, actor(), token)


@pytest.mark.parametrize("fault", ["audit", "revoke", "expiry", "deadline"])
def test_mid_read_failures_do_not_deliver_metadata_and_recover(tmp_path, monkeypatch, fault):
    now = [datetime(2026, 11, 3, tzinfo=UTC)]
    service = build_service(tmp_path, clock=lambda: now[0], retention=2)
    token, headers = credentials(service.broker)
    stage_many(service, token, 1)
    emit = service.audit.emit

    def fail(event):
        if fault == "audit":
            raise OSError("private-store-path")
        emit(event)
        if fault == "revoke":
            service.broker.revoke(token)
        elif fault == "expiry":
            now[0] += timedelta(seconds=2)
        else:
            service.monotonic = lambda: float("inf")

    with monkeypatch.context() as patch:
        patch.setattr(service.audit, "emit", fail)
        response = client_for(service).get(URL, headers=headers)
        if fault == "expiry":
            assert response.status_code == 200 and response.json()["items"] == []
        else:
            assert response.status_code == {"audit": 503, "revoke": 404, "deadline": 413}[fault]
        assert "private-store-path" not in response.text
    service.monotonic = csv_intake.monotonic
    fresh_token, _ = credentials(service.broker)
    page = CsvFileListing(service).list_files(actor(), fresh_token)
    assert len(page.items) == (0 if fault == "expiry" else 1)


def test_owner_corruption_fails_closed_other_scope_isolated_and_schema_recovers(tmp_path):
    service = build_service(tmp_path)
    token, headers = credentials(service.broker)
    previews = stage_many(service, token, 2)
    file_id = str(previews[1].file.file_id)
    with service.store.db(write=True) as db:
        original = db.execute(
            "SELECT body FROM csv_uploads WHERE file_id=?", (file_id,)
        ).fetchone()[0]
        db.execute("UPDATE csv_uploads SET body=? WHERE file_id=?", (b"corrupt", file_id))
    assert client_for(service).get(URL, headers=headers).status_code == 404
    other = actor().model_copy(update={"user_id": "other"})
    other_token, _ = credentials(service.broker, other)
    assert CsvFileListing(service).list_files(other, other_token).items == ()
    with service.store.db(write=True) as db:
        db.execute("UPDATE csv_uploads SET body=? WHERE file_id=?", (original, file_id))
    assert len(CsvFileListing(service).list_files(actor(), token).items) == 2
    with service.store.db(write=True) as db:
        db.execute("PRAGMA user_version=2")
    assert client_for(service).get(URL, headers=headers).status_code == 503
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(service.store.path)) as db:
        db.execute("PRAGMA user_version=1")
    assert len(CsvFileListing(service).list_files(actor(), token).items) == 2


def test_registered_status_is_not_report_or_artifact_integrity_claim(tmp_path):
    service = build_service(tmp_path)
    token, _ = credentials(service.broker)
    preview = stage_many(service, token, 1)[0]
    service.confirm(preview.file.file_id, actor(), token, confirmation(preview))
    with service.store.db(write=True) as db:
        db.execute("UPDATE csv_uploads SET report=?", (b"damaged-report",))
    assert CsvFileListing(service).list_files(actor(), token).items[0].status == "profiled"
    with pytest.raises(CsvIntakeConflict):
        service.result(preview.file.file_id, actor(), token)
    assert len(list(service.artifact_root.rglob("*.json"))) == 2


@pytest.mark.parametrize("limit", [True, 0, 21, -1, 1.0, "1"])
def test_service_strict_page_bounds(tmp_path, limit):
    service = build_service(tmp_path)
    token, _ = credentials(service.broker)
    with pytest.raises(ValueError):
        CsvFileListing(service).list_files(actor(), token, limit=limit)


def test_tightened_quota_still_lists_retained_inventory_and_over_ceiling_denies(tmp_path):
    service = build_service(tmp_path)
    token, _ = credentials(service.broker)
    stage_many(service, token, 20)
    service.store.max_files_per_actor = 1
    assert len(CsvFileListing(service).list_files(actor(), token, limit=20).items) == 20
    service.store.max_files_per_actor = 21  # Simulate an invalid adapter/retained inventory.
    service.stage(RAW, actor(), token, "over-hard-ceiling")
    with pytest.raises(CsvIntakeUnavailable):
        CsvFileListing(service).list_files(actor(), token)


def test_api_requires_current_identity_permission_and_bounded_parameters(tmp_path):
    from fastapi.testclient import TestClient

    from generaldataagent.api.app import create_app

    service = build_service(tmp_path)
    token, headers = credentials(service.broker)
    client = client_for(service)
    assert client.get(URL).status_code == 401
    _, denied = credentials(service.broker, permissions=frozenset())
    assert client.get(URL, headers=denied).status_code == 403
    assert client.get(URL, headers=headers | {"x-project-id": "other"}).status_code == 403
    for params in [{"limit": 0}, {"limit": 21}, {"limit": "yes"}]:
        assert client.get(URL, headers=headers, params=params).status_code == 422
    assert TestClient(create_app()).get(URL, headers=headers).status_code == 503
    service.broker.revoke(token)
    assert client.get(URL, headers=headers).status_code == 401


def test_api_cursor_continuation_is_opaque_and_empty_inventory_audited(tmp_path):
    service = build_service(tmp_path)
    token, headers = credentials(service.broker)
    client = client_for(service)
    assert client.get(URL, headers=headers).json() == {
        "schema_version": "1.0",
        "items": [],
        "next_cursor": None,
    }
    previews = stage_many(service, token)
    first = client.get(URL, headers=headers, params={"limit": 1}).json()
    assert first["next_cursor"] and not any(
        str(p.file.file_id) in first["next_cursor"] for p in previews
    )
    seen, page = [], first
    while True:
        seen.extend(i["file_id"] for i in page["items"])
        if page["next_cursor"] is None:
            break
        response = client.get(
            URL, headers=headers, params={"limit": 1, "cursor": page["next_cursor"]}
        )
        assert response.status_code == 200
        page = response.json()
    assert seen == [str(p.file.file_id) for p in previews]


def test_cursor_expiry_during_audit_denies_page_and_clock_rollback_denies_cursor(
    tmp_path, monkeypatch
):
    now = [datetime(2026, 11, 3, tzinfo=UTC)]
    service = build_service(tmp_path, clock=lambda: now[0])
    token, _ = credentials(service.broker)
    stage_many(service, token)
    listing = CsvFileListing(service)
    first = listing.list_files(actor(), token, limit=1)
    now[0] -= timedelta(seconds=1)
    with pytest.raises(CsvIntakeConflict):
        listing.list_files(actor(), token, limit=1, cursor=first.next_cursor)
    now[0] += timedelta(seconds=1)
    with monkeypatch.context() as patch:
        patch.setattr(
            service.audit, "emit", lambda _: now.__setitem__(0, now[0] + timedelta(minutes=5))
        )
        with pytest.raises(CsvIntakeConflict):
            listing.list_files(actor(), token, limit=1, cursor=first.next_cursor)
    assert len(listing.list_files(actor(), token).items) == 3


def test_current_grant_is_rechecked_during_inventory_decode(tmp_path, monkeypatch):
    service = build_service(tmp_path)
    token, _ = credentials(service.broker)
    stage_many(service, token)
    decode = service.store.decode

    def revoked(row):
        payload = decode(row)
        service.broker.revoke(token)
        return payload

    with monkeypatch.context() as patch:
        patch.setattr(service.store, "decode", revoked)
        with pytest.raises(PermissionError):
            CsvFileListing(service).list_files(actor(), token)
    fresh, _ = credentials(service.broker)
    assert len(CsvFileListing(service).list_files(actor(), fresh).items) == 3
