import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from test_metric_analysis import actor
from test_stage11_task_submission import credentials
from test_stage13_xlsx_conformance import prepared
from test_stage13_xlsx_intake import (
    build_service,
    client_for,
    confirm,
    confirm_body,
    declaration_body,
    declare,
    upload,
)
from test_stage13_xlsx_interpretation import declare as declared_intent
from test_stage13_xlsx_preflight import package
from test_stage13_xlsx_preview import entries_for, preview, row, sheet, text_cell

from generaldataagent.datasets import xlsx_intake, xlsx_snapshot
from generaldataagent.datasets.xlsx_intake_contracts import XlsxSnapshotColumnData, XlsxSnapshotData
from generaldataagent.datasets.xlsx_snapshot import (
    XlsxSnapshotColumnRef,
    XlsxSnapshotPage,
    XlsxSnapshotRow,
    derive_snapshot,
    scan_snapshot_values,
)
from generaldataagent.security.dlp import DLPDeniedError


def snapshot(client, headers, file_id, *, method="post"):
    return getattr(client, method)(f"/v1/analysis/files/xlsx/{file_id}/snapshot", headers=headers)


def test_snapshot_retains_declared_values_without_returning_them(tmp_path):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    client = client_for(service)
    file_id, _ = prepared(client, headers)
    response = snapshot(client, headers, file_id)
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["scope"] == "whole-sheet-declared-columns"
    assert report["rows"] == "present-rows-only"
    assert report["values"] == "retained-encrypted-not-returned"
    assert report["storage"] == "encrypted-derived-not-artifact"
    assert report["dlp"] == "extracted-values-scanned"
    assert report["dataset_snapshot"] is False
    assert report["artifact"] is False
    assert report["formula_cached_values"] == "never-trusted"
    assert report["duplicate_rows_handling"] == "counted-not-dropped"
    assert report["status"] == "current"
    assert report["data_rows"] == 7
    assert report["cells"] == 21
    assert report["duplicate_rows"] == "reject"
    assert report["duplicate_row_count"] == 1
    name, amount, flag = report["columns"]
    assert (name["cells"], name["values"], name["missing"]) == (7, 6, 1)
    assert (amount["values"], amount["unparsed"], amount["not_trusted"]) == (4, 2, 1)
    assert (flag["values"], flag["unparsed"]) == (6, 1)
    body = json.dumps(report)
    for secret in ("alpha", "beta", "gamma", "delta", "2.5", "#DIV/0!"):
        assert secret not in body
    assert snapshot(client, headers, file_id, method="get").json() == report
    assert (tmp_path / "audit.jsonl").read_text().count("xlsx.snapshot_derived") == 1
    assert b"alpha" not in (tmp_path / "xlsx.db").read_bytes()


def test_snapshot_replay_is_idempotent_and_not_rewritten(tmp_path):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    client = client_for(service)
    file_id, _ = prepared(client, headers)
    first = snapshot(client, headers, file_id).json()
    second = snapshot(client, headers, file_id).json()
    assert first == second
    assert (tmp_path / "audit.jsonl").read_text().count("xlsx.snapshot_derived") == 1


def test_snapshot_requires_a_declaration_and_declared_columns(tmp_path):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    client = client_for(service)
    staged = upload(client, headers).json()
    file_id = staged["file"]["file_id"]
    early = snapshot(client, headers, file_id)
    assert early.status_code == 409
    assert early.json()["detail"] == "xlsx_interpretation_not_declared"
    assert snapshot(client, headers, file_id, method="get").status_code == 409
    confirmed = confirm(client, headers, file_id, confirm_body(staged)).json()
    empty = declare(client, headers, file_id, declaration_body(confirmed, columns=[]))
    assert empty.status_code == 200, empty.text
    response = snapshot(client, headers, file_id)
    assert response.status_code == 409
    assert response.json()["detail"] == "xlsx_snapshot_no_declared_columns"
    missing = snapshot(client, headers, file_id, method="get")
    assert missing.status_code == 409
    assert missing.json()["detail"] == "xlsx_snapshot_not_derived"


def test_snapshot_is_creator_scoped_and_expires(tmp_path):
    now = [datetime.now(UTC)]
    service = build_service(tmp_path, clock=lambda: now[0], retention=60)
    _, headers = credentials(service.broker)
    client = client_for(service)
    file_id, _ = prepared(client, headers)
    assert snapshot(client, headers, file_id).status_code == 200
    other = actor().model_copy(update={"user_id": "other", "tenant_id": "tenant-2"})
    _, other_headers = credentials(service.broker, other)
    assert snapshot(client, other_headers, file_id, method="get").status_code == 404
    now[0] += timedelta(seconds=61)
    assert snapshot(client, headers, file_id, method="get").status_code == 404


def test_snapshot_fails_closed_on_parser_drift(tmp_path, monkeypatch):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    client = client_for(service)
    file_id, _ = prepared(client, headers)
    monkeypatch.setattr(xlsx_intake, "parser_fingerprint", lambda *args: "sha256:" + "1" * 64)
    response = snapshot(client, headers, file_id)
    assert response.status_code == 409
    assert response.json()["detail"] == "xlsx_parser_or_limits_changed_restage_required"
    assert "xlsx.snapshot_derived" not in (tmp_path / "audit.jsonl").read_text()


def test_snapshot_scans_values_outside_the_preview_window(tmp_path):
    class BlockingScanner:
        def check(self, content: bytes) -> None:
            if b"outside-secret" in content:
                raise DLPDeniedError("dlp_personal_data")

    raw = package(
        entries_for(
            sheet(
                row(1, text_cell("A1", "name")),
                *[row(number, text_cell(f"A{number}", f"row{number}")) for number in range(2, 7)],
                row(7, text_cell("A7", "outside-secret")),
            )
        )
    )
    service = build_service(tmp_path, scanner=BlockingScanner())
    _, headers = credentials(service.broker)
    client = client_for(service)
    staged = upload(client, headers, raw=raw).json()
    file_id = staged["file"]["file_id"]
    confirmed = confirm(client, headers, file_id, confirm_body(staged)).json()
    declare(
        client,
        headers,
        file_id,
        declaration_body(confirmed, columns=[{"index": 0, "header": "name", "type": "text"}]),
    )
    response = snapshot(client, headers, file_id)
    assert response.status_code == 404
    assert response.json()["detail"] == "xlsx_file_unavailable"
    assert snapshot(client, headers, file_id, method="get").status_code == 409
    assert "xlsx.snapshot_derived" not in (tmp_path / "audit.jsonl").read_text()


def test_snapshot_enforces_cell_and_character_budgets(tmp_path, monkeypatch):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    client = client_for(service)
    file_id, _ = prepared(client, headers)
    monkeypatch.setattr(xlsx_snapshot, "MAX_SNAPSHOT_CELLS", 2)
    response = snapshot(client, headers, file_id)
    assert response.status_code == 413
    assert response.json()["detail"] == "xlsx_snapshot_cell_limit"
    monkeypatch.setattr(xlsx_snapshot, "MAX_SNAPSHOT_CELLS", 250_000)
    monkeypatch.setattr(xlsx_snapshot, "MAX_SNAPSHOT_CHARS", 5)
    response = snapshot(client, headers, file_id)
    assert response.status_code == 413
    assert response.json()["detail"] == "xlsx_snapshot_char_limit"


def test_snapshot_becomes_stale_after_a_new_declaration(tmp_path):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    client = client_for(service)
    file_id, declared = prepared(client, headers)
    first = snapshot(client, headers, file_id).json()
    revised = declare(
        client,
        headers,
        file_id,
        declaration_body(
            declared,
            columns=[{"index": 0, "header": "name", "type": "text"}],
            duplicate_rows="keep-all",
        ),
    )
    assert revised.status_code == 200, revised.text
    stale = snapshot(client, headers, file_id, method="get").json()
    assert stale["status"] == "stale"
    assert stale["snapshot_hash"] == first["snapshot_hash"]
    assert stale["interpretation_hash"] == first["interpretation_hash"]
    assert stale["duplicate_rows"] == "reject"
    refreshed = snapshot(client, headers, file_id).json()
    assert refreshed["status"] == "current"
    assert refreshed["snapshot_hash"] != first["snapshot_hash"]
    assert refreshed["duplicate_rows"] == "keep-all"
    assert refreshed["cells"] == 7
    assert len(refreshed["columns"]) == 1


def test_snapshot_derivation_rejects_drifted_bindings():
    small = preview(sheet(row(1, text_cell("A1", "name")), row(2, text_cell("A2", "alpha")))).data
    interpretation = declared_intent(small, [{"index": 0, "header": "name", "type": "text"}])
    with pytest.raises(ValueError, match="xlsx_snapshot_binding_invalid"):
        derive_snapshot(
            package(
                entries_for(
                    sheet(
                        row(1, text_cell("A1", "name")),
                        row(2, text_cell("A2", "alpha")),
                        row(3, text_cell("A3", "extra")),
                    )
                )
            ),
            preview=small,
            interpretation=interpretation,
            date_system="1900",
            created_at=datetime.now(UTC),
            check=lambda: None,
        )
    with pytest.raises(ValueError, match="xlsx_snapshot_columns_required"):
        derive_snapshot(
            package(entries_for(sheet(row(1, text_cell("A1", "name"))))),
            preview=small,
            interpretation=declared_intent(small, []),
            date_system="1900",
            created_at=datetime.now(UTC),
            check=lambda: None,
        )


def test_snapshot_contracts_hold_invariants():
    with pytest.raises(ValidationError, match="xlsx_snapshot_counts_invalid"):
        XlsxSnapshotColumnData(index=0, header="name", type="text", statuses="vu", values=())
    with pytest.raises(ValidationError, match="xlsx_snapshot_value_invalid"):
        XlsxSnapshotColumnData(index=0, header="name", type="text", statuses="v", values=("",))
    base = {
        "snapshot_hash": "sha256:" + "0" * 64,
        "source_hash": "sha256:" + "1" * 64,
        "preview_hash": "sha256:" + "2" * 64,
        "confirmation_hash": "sha256:" + "3" * 64,
        "interpretation_hash": "sha256:" + "4" * 64,
        "created_at": datetime.now(UTC),
        "header_row": 1,
        "date_system": "1900",
        "duplicate_rows": "reject",
        "duplicate_row_count": 0,
        "columns": (
            XlsxSnapshotColumnData(
                index=0, header="name", type="text", statuses="v", values=("a",)
            ),
        ),
    }
    with pytest.raises(ValidationError, match="xlsx_snapshot_rows_invalid"):
        XlsxSnapshotData(rows=(3, 2), **base)
    assert XlsxSnapshotData(rows=(2,), **base).reader == "gda-xlsx-derived-snapshot-v1"


def test_snapshot_value_scan_rejects_denied_text():
    class BlockingScanner:
        def check(self, content: bytes) -> None:
            raise DLPDeniedError("dlp_personal_data")

    snapshot_data = XlsxSnapshotData(
        snapshot_hash="sha256:" + "0" * 64,
        source_hash="sha256:" + "1" * 64,
        preview_hash="sha256:" + "2" * 64,
        confirmation_hash="sha256:" + "3" * 64,
        interpretation_hash="sha256:" + "4" * 64,
        created_at=datetime.now(UTC),
        header_row=1,
        date_system="1900",
        rows=(2,),
        columns=(
            XlsxSnapshotColumnData(
                index=0, header="name", type="text", statuses="v", values=("a",)
            ),
        ),
        duplicate_rows="reject",
        duplicate_row_count=0,
    )
    with pytest.raises(DLPDeniedError):
        scan_snapshot_values(snapshot_data, BlockingScanner(), lambda: None)


def values(client, headers, file_id, **params):
    return client.get(
        f"/v1/analysis/files/xlsx/{file_id}/snapshot/values", headers=headers, params=params
    )


def test_snapshot_values_return_declared_columns_only(tmp_path):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    client = client_for(service)
    file_id, _ = prepared(client, headers)
    assert snapshot(client, headers, file_id).status_code == 200
    response = values(client, headers, file_id, limit=500)
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["reader"] == "gda-xlsx-snapshot-values-v1"
    assert page["status"] == "current"
    assert page["retained_rows"] == 7
    assert page["rows"] and len(page["rows"]) == 7
    assert [row["row"] for row in page["rows"]] == list(range(2, 9))
    assert [column["header"] for column in page["columns"]] == ["name", "amount", "flag"]
    assert page["more"] is False and page["next"] is None and page["after"] is None
    assert page["values"] == "retained-decrypted-declared-columns-only"
    assert page["dlp"] == "values-rescanned-before-return"
    assert page["dataset_snapshot"] is False and page["artifact"] is False
    first, second = page["rows"][0], page["rows"][1]
    assert first["statuses"] == "vvv"
    assert first["values"] == ["alpha", "1", "true"]
    assert second["statuses"] == "vtv"
    assert second["values"] == ["beta", None, "false"]
    assert page["rows"][3]["statuses"] == "vuu"
    assert page["rows"][3]["values"] == ["gamma", None, None]
    assert page["rows"][6]["statuses"] == "mvv" and page["rows"][6]["values"] == [None, "8", "true"]
    assert (tmp_path / "audit.jsonl").read_text().count("xlsx.snapshot_values_read") == 1


def test_snapshot_values_page_by_keyset_without_overlap(tmp_path):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    client = client_for(service)
    file_id, _ = prepared(client, headers)
    assert snapshot(client, headers, file_id).status_code == 200
    seen, after = [], None
    while True:
        params = {"limit": 3} | ({"after": after} if after else {})
        page = values(client, headers, file_id, **params).json()
        seen.extend(row["row"] for row in page["rows"])
        if not page["more"]:
            assert page["next"] is None
            break
        assert page["next"] == page["rows"][-1]["row"]
        after = page["next"]
    assert seen == list(range(2, 9))
    empty = values(client, headers, file_id, after=8).json()
    assert empty["rows"] == [] and empty["more"] is False and empty["next"] is None
    assert empty["retained_rows"] == 7
    audit = (tmp_path / "audit.jsonl").read_text()
    assert audit.count("xlsx.snapshot_values_read") == 3


def test_snapshot_values_fail_closed_when_stale_or_absent(tmp_path):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    client = client_for(service)
    staged = upload(client, headers).json()
    file_id = staged["file"]["file_id"]
    assert values(client, headers, file_id).status_code == 409
    confirmed = confirm(client, headers, file_id, confirm_body(staged)).json()
    declared = declare(
        client,
        headers,
        file_id,
        declaration_body(confirmed, columns=[{"index": 0, "header": "name", "type": "text"}]),
    ).json()
    assert values(client, headers, file_id).json()["detail"] == "xlsx_snapshot_not_derived"
    assert snapshot(client, headers, file_id).status_code == 200
    revised = declare(
        client,
        headers,
        file_id,
        declaration_body(
            declared,
            columns=[{"index": 0, "header": "name", "type": "text"}],
            duplicate_rows="keep-all",
        ),
    )
    assert revised.status_code == 200, revised.text
    stale = values(client, headers, file_id)
    assert stale.status_code == 409
    assert stale.json()["detail"] == "xlsx_snapshot_stale"
    assert snapshot(client, headers, file_id).status_code == 200
    assert values(client, headers, file_id).status_code == 200


def test_snapshot_values_are_creator_scoped(tmp_path):
    now = [datetime.now(UTC)]
    service = build_service(tmp_path, clock=lambda: now[0], retention=60)
    _, headers = credentials(service.broker)
    client = client_for(service)
    file_id, _ = prepared(client, headers)
    assert snapshot(client, headers, file_id).status_code == 200
    other = actor().model_copy(update={"user_id": "other", "tenant_id": "tenant-2"})
    _, other_headers = credentials(service.broker, other)
    assert values(client, other_headers, file_id).status_code == 404
    now[0] += timedelta(seconds=61)
    assert values(client, headers, file_id).status_code == 404


def test_snapshot_values_are_rescanned_before_return(tmp_path):
    class LateScanner:
        def __init__(self):
            self.deny = False

        def check(self, content: bytes) -> None:
            if self.deny and b"one" in content:
                raise DLPDeniedError("dlp_personal_data")

    scanner = LateScanner()
    service = build_service(tmp_path, scanner=scanner)
    _, headers = credentials(service.broker)
    client = client_for(service)
    file_id, _ = prepared(client, headers)
    assert snapshot(client, headers, file_id).status_code == 200
    scanner.deny = True
    response = values(client, headers, file_id)
    assert response.status_code == 404
    assert response.json()["detail"] == "xlsx_file_unavailable"
    assert "xlsx.snapshot_values_read" not in (tmp_path / "audit.jsonl").read_text()


def test_snapshot_values_enforce_the_page_character_budget(tmp_path, monkeypatch):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    client = client_for(service)
    file_id, _ = prepared(client, headers)
    assert snapshot(client, headers, file_id).status_code == 200
    monkeypatch.setattr(xlsx_snapshot, "MAX_PAGE_CHARS", 5)
    response = values(client, headers, file_id, limit=1)
    assert response.status_code == 413
    assert response.json()["detail"] == "xlsx_snapshot_page_char_limit"


def test_snapshot_page_contract_holds_invariants():
    with pytest.raises(ValidationError, match="xlsx_snapshot_page_invalid"):
        XlsxSnapshotRow(row=2, statuses="vv", values=("a",))
    row = XlsxSnapshotRow(row=2, statuses="vm", values=("a", None))
    with pytest.raises(ValidationError, match="xlsx_snapshot_page_invalid"):
        XlsxSnapshotPage(
            snapshot_hash="sha256:" + "0" * 64,
            interpretation_hash="sha256:" + "1" * 64,
            columns=(XlsxSnapshotColumnRef(index=0, header="name", type="text"),),
            rows=(row,),
            more=True,
            retained_rows=1,
        )
