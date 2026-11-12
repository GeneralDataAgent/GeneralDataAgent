import json
from datetime import UTC, datetime, timedelta

import pytest
from test_metric_analysis import actor
from test_stage11_task_submission import credentials
from test_stage13_xlsx_intake import (
    build_service,
    client_for,
    confirm,
    confirm_body,
    declaration_body,
    declare,
    upload,
)
from test_stage13_xlsx_interpretation import bool_cell, error_cell
from test_stage13_xlsx_interpretation import declare as declared_intent
from test_stage13_xlsx_preflight import package
from test_stage13_xlsx_preview import (
    entries_for,
    formula_cell,
    number_cell,
    preview,
    row,
    sheet,
    text_cell,
)

from generaldataagent.datasets import xlsx_conformance, xlsx_intake
from generaldataagent.datasets.xlsx_conformance import conform_declared_columns

COLUMNS = [
    {"index": 0, "header": "name", "type": "text", "missing_tokens": ["NA"]},
    {"index": 1, "header": "amount", "type": "integer"},
    {"index": 2, "header": "flag", "type": "boolean"},
]
RAW = package(
    entries_for(
        sheet(
            row(1, text_cell("A1", "name"), text_cell("B1", "amount"), text_cell("C1", "flag")),
            row(2, text_cell("A2", "alpha"), number_cell("B2", "1"), bool_cell("C2", "1")),
            row(
                3,
                text_cell("A3", "beta"),
                formula_cell("B3", "1+1", cached="2"),
                bool_cell("C3", "0"),
            ),
            row(4, text_cell("A4", "alpha"), number_cell("B4", "1"), bool_cell("C4", "1")),
            row(5, text_cell("A5", "gamma"), error_cell("B5", "#DIV/0!"), bool_cell("C5", "2")),
            row(6, text_cell("A6", "delta"), number_cell("B6", "2.5"), bool_cell("C6", "1")),
            row(7, text_cell("A7", "one"), number_cell("B7", "7"), bool_cell("C7", "0")),
            row(8, text_cell("A8", "NA"), number_cell("B8", "8"), bool_cell("C8", "1")),
        )
    )
)


def conformance(client, headers, file_id):
    return client.get(f"/v1/analysis/files/xlsx/{file_id}/conformance", headers=headers)


def prepared(client, headers, *, raw=RAW, columns=COLUMNS):
    preview = upload(client, headers, raw=raw).json()
    file_id = preview["file"]["file_id"]
    confirmed = confirm(client, headers, file_id, confirm_body(preview)).json()
    response = declare(client, headers, file_id, declaration_body(confirmed, columns=columns))
    assert response.status_code == 200, response.text
    return file_id, response.json()


def test_conformance_covers_rows_beyond_the_preview_window(tmp_path):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    client = client_for(service)
    file_id, declared = prepared(client, headers)
    window = client.get(f"/v1/analysis/files/xlsx/{file_id}/interpretation", headers=headers).json()
    assert [len(column["cells"]) for column in window["columns"]] == [5, 5, 5]
    response = conformance(client, headers, file_id)
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["data_rows"] == 7
    assert report["cells_read"] == 21
    assert report["duplicate_rows"] == "reject"
    assert report["duplicate_row_count"] == 1
    assert report["scope"] == "whole-sheet-declared-columns"
    assert report["rows"] == "present-rows-only"
    assert report["values"] == "digest-and-counts-not-returned"
    assert report["formula_cached_values"] == "never-trusted"
    assert report["duplicate_rows_handling"] == "counted-not-dropped"
    assert report["storage"] == "derived-not-persisted"
    name, amount, flag = report["columns"]
    assert (name["cells"], name["values"], name["missing"]) == (7, 6, 1)
    assert name["reasons"] == [{"schema_version": "1.0", "reason": "missing_token", "count": 1}]
    assert (amount["values"], amount["unparsed"], amount["not_trusted"]) == (4, 2, 1)
    assert [item["reason"] for item in amount["reasons"]] == [
        "error_cell",
        "formula_cached_value_not_trusted",
        "non_integral_number",
    ]
    assert (flag["values"], flag["unparsed"]) == (6, 1)
    assert [item["reason"] for item in flag["reasons"]] == ["invalid_boolean"]
    body = json.dumps(report)
    for secret in ("alpha", "beta", "gamma", "delta", "NA", "2.5", "#DIV/0!"):
        assert secret not in body
    assert conformance(client, headers, file_id).json() == report
    assert (tmp_path / "audit.jsonl").read_text().count("xlsx.conformance") == 0


def test_conformance_requires_a_retained_declaration(tmp_path):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    client = client_for(service)
    staged = upload(client, headers, raw=RAW).json()
    file_id = staged["file"]["file_id"]
    early = conformance(client, headers, file_id)
    assert early.status_code == 409
    assert early.json()["detail"] == "xlsx_interpretation_not_declared"
    confirmed = confirm(client, headers, file_id, confirm_body(staged)).json()
    empty = declare(client, headers, file_id, declaration_body(confirmed, columns=[]))
    assert empty.status_code == 200, empty.text
    response = conformance(client, headers, file_id)
    assert response.status_code == 409
    assert response.json()["detail"] == "xlsx_conformance_no_declared_columns"


def test_conformance_is_creator_scoped_and_expires(tmp_path):
    now = [datetime.now(UTC)]
    service = build_service(tmp_path, clock=lambda: now[0], retention=60)
    _, headers = credentials(service.broker)
    client = client_for(service)
    file_id, _ = prepared(client, headers)
    other = actor().model_copy(update={"user_id": "other"})
    _, other_headers = credentials(service.broker, other)
    assert conformance(client, other_headers, file_id).status_code == 404
    now[0] += timedelta(seconds=61)
    assert conformance(client, headers, file_id).status_code == 404


def test_conformance_fails_closed_on_parser_drift(tmp_path):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    client = client_for(service)
    file_id, _ = prepared(client, headers)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(xlsx_intake, "parser_fingerprint", lambda *args: "sha256:" + "1" * 64)
    try:
        response = conformance(client, headers, file_id)
        assert response.status_code == 409
        assert response.json()["detail"] == "xlsx_parser_or_limits_changed_restage_required"
    finally:
        monkeypatch.undo()


def test_conformance_enforces_cell_and_character_budgets(tmp_path, monkeypatch):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    client = client_for(service)
    file_id, _ = prepared(client, headers)
    monkeypatch.setattr(xlsx_conformance, "MAX_CONFORMANCE_CELLS", 2)
    response = conformance(client, headers, file_id)
    assert response.status_code == 413
    assert response.json()["detail"] == "xlsx_conformance_cell_limit"
    monkeypatch.setattr(xlsx_conformance, "MAX_CONFORMANCE_CELLS", 250_000)
    monkeypatch.setattr(xlsx_conformance, "MAX_CONFORMANCE_CHARS", 5)
    response = conformance(client, headers, file_id)
    assert response.status_code == 413
    assert response.json()["detail"] == "xlsx_conformance_char_limit"


def test_conformance_rejects_a_row_count_that_drifted_from_the_preview():
    small = preview(sheet(row(1, text_cell("A1", "name")), row(2, text_cell("A2", "alpha")))).data
    interpretation = declared_intent(small, [{"index": 0, "header": "name", "type": "text"}])
    with pytest.raises(ValueError, match="xlsx_conformance_binding_invalid"):
        conform_declared_columns(
            RAW,
            preview=small,
            interpretation=interpretation,
            date_system="1900",
            check=lambda: None,
        )
