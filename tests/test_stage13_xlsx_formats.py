import json
from datetime import UTC, datetime, timedelta
from xml.sax.saxutils import escape

import pytest
from test_metric_analysis import actor
from test_stage11_task_submission import credentials
from test_stage13_xlsx_conformance import COLUMNS
from test_stage13_xlsx_intake import (
    XLSX,
    build_service,
    client_for,
    confirm,
    confirm_body,
    declaration_body,
    declare,
)
from test_stage13_xlsx_interpretation import declare as declared_intent
from test_stage13_xlsx_preflight import package
from test_stage13_xlsx_preview import (
    entries_for,
    number_cell,
    preview,
    row,
    sheet,
    text_cell,
)

from generaldataagent.datasets import xlsx_formats, xlsx_intake
from generaldataagent.datasets.xlsx_formats import (
    XlsxColumnFormats,
    XlsxFormatCode,
    XlsxFormatReport,
    read_declared_formats,
)
from generaldataagent.datasets.xlsx_intake_contracts import XlsxIntakeLimit
from generaldataagent.security.dlp import DLPDeniedError

CURRENCY = '"$"#,##0.00'


def styled(cell, style):
    return cell.replace(' r="', f' s="{style}" r="', 1)


def styles_xml(*num_fmts, xfs):
    codes = "".join(
        f'<numFmt numFmtId="{num_fmt_id}" formatCode="{escape(code, {chr(34): "&quot;"})}"/>'
        for num_fmt_id, code in num_fmts
    )
    attributes = ["" if item is None else f' numFmtId="{item}"' for item in xfs]
    cells = "".join(f"<xf{value}/>" for value in attributes)
    return (
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<numFmts count="{len(num_fmts)}">{codes}</numFmts>'
        f'<cellXfs count="{len(xfs)}">{cells}</cellXfs></styleSheet>'
    )


STYLES = styles_xml((164, CURRENCY), xfs=(0, 9, 164))
ROWS = (
    ("alpha", 2, "1"),
    ("beta", 2, "0"),
    ("gamma", 2, "1"),
    ("delta", 1, "1"),
    ("one", 1, "0"),
    ("two", None, "1"),
    ("three", None, None),
)


def data_row(number, name, style, flag):
    amount = number_cell(f"B{number}", str(number))
    cells = [text_cell(f"A{number}", name)]
    cells.append(amount if style is None else styled(amount, style))
    if flag is not None:
        cells.append(number_cell(f"C{number}", flag))
    return row(number, *cells)


RAW = package(
    entries_for(
        sheet(
            row(1, text_cell("A1", "name"), text_cell("B1", "amount"), text_cell("C1", "flag")),
            *[data_row(number, *values) for number, values in enumerate(ROWS, start=2)],
        ),
        extra=[("xl/styles.xml", STYLES.encode())],
    )
)
RAW_PLAIN = package(
    entries_for(
        sheet(
            row(1, text_cell("A1", "name"), text_cell("B1", "amount"), text_cell("C1", "flag")),
            *[
                row(
                    number,
                    text_cell(f"A{number}", f"row{number}"),
                    number_cell(f"B{number}", str(number)),
                    number_cell(f"C{number}", str(number % 2)),
                )
                for number in range(2, 9)
            ],
        )
    )
)
HEADERS = {
    "content-type": XLSX,
    "idempotency-key": "xlsx-formats",
}


def formats(client, headers, file_id):
    return client.get(f"/v1/analysis/files/xlsx/{file_id}/formats", headers=headers)


def stage(client, headers, raw, key="xlsx-formats"):
    return client.post(
        "/v1/analysis/files/xlsx", headers=headers | HEADERS | {"idempotency-key": key}, content=raw
    )


def prepared_with(client, headers, raw=RAW):
    staged = stage(client, headers, raw).json()
    file_id = staged["file"]["file_id"]
    confirmed = confirm(client, headers, file_id, confirm_body(staged)).json()
    response = declare(client, headers, file_id, declaration_body(confirmed, columns=COLUMNS))
    assert response.status_code == 200, response.text
    return file_id


def test_formats_count_number_formats_beyond_the_preview_window(tmp_path):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    client = client_for(service)
    file_id = prepared_with(client, headers)
    window = client.get(f"/v1/analysis/files/xlsx/{file_id}/interpretation", headers=headers).json()
    assert [len(column["cells"]) for column in window["columns"]] == [5, 5, 5]
    response = formats(client, headers, file_id)
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["scope"] == "whole-sheet-declared-columns"
    assert report["rows"] == "present-rows-only"
    assert report["display_format"] == "inert-not-applied"
    assert report["currencies"] == "not-interpreted"
    assert report["percentages"] == "not-interpreted"
    assert report["times_of_day"] == "not-interpreted"
    assert report["classification"] == "not-evaluated"
    assert report["values"] == "unchanged"
    assert report["dlp"] == "format-codes-scanned"
    assert report["storage"] == "derived-not-persisted"
    name, amount, flag = report["columns"]
    assert name["cells"] == amount["cells"] == flag["cells"] == 7
    assert name["formats"] == [
        {"schema_version": "1.0", "num_fmt_id": 0, "kind": "builtin", "code": None, "cells": 7}
    ]
    assert amount["formats"] == [
        {"schema_version": "1.0", "num_fmt_id": 0, "kind": "builtin", "code": None, "cells": 2},
        {"schema_version": "1.0", "num_fmt_id": 9, "kind": "builtin", "code": None, "cells": 2},
        {
            "schema_version": "1.0",
            "num_fmt_id": 164,
            "kind": "custom",
            "code": CURRENCY,
            "cells": 3,
        },
    ]
    assert flag["formats"][0]["cells"] == 7
    body = json.dumps(report)
    for secret in ("alpha", "beta", "gamma", "delta", "three", "0.25"):
        assert secret not in body
    assert formats(client, headers, file_id).json() == report
    assert (tmp_path / "audit.jsonl").read_text().count("xlsx.formats") == 0


def test_formats_default_to_general_when_the_workbook_has_no_styles(tmp_path):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    client = client_for(service)
    file_id = prepared_with(client, headers, raw=RAW_PLAIN)
    report = formats(client, headers, file_id).json()
    for column in report["columns"]:
        assert column["formats"] == [
            {"schema_version": "1.0", "num_fmt_id": 0, "kind": "builtin", "code": None, "cells": 7}
        ]


def test_formats_require_a_retained_declaration(tmp_path):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    client = client_for(service)
    staged = stage(client, headers, RAW, key="xlsx-formats-empty").json()
    file_id = staged["file"]["file_id"]
    early = formats(client, headers, file_id)
    assert early.status_code == 409
    assert early.json()["detail"] == "xlsx_interpretation_not_declared"
    confirmed = confirm(client, headers, file_id, confirm_body(staged)).json()
    empty = declare(client, headers, file_id, declaration_body(confirmed, columns=[]))
    assert empty.status_code == 200, empty.text
    response = formats(client, headers, file_id)
    assert response.status_code == 409
    assert response.json()["detail"] == "xlsx_formats_no_declared_columns"


def test_formats_are_creator_scoped_and_expire(tmp_path):
    now = [datetime.now(UTC)]
    service = build_service(tmp_path, clock=lambda: now[0], retention=60)
    _, headers = credentials(service.broker)
    client = client_for(service)
    file_id = prepared_with(client, headers)
    other = actor().model_copy(update={"user_id": "other"})
    _, other_headers = credentials(service.broker, other)
    assert formats(client, other_headers, file_id).status_code == 404
    now[0] += timedelta(seconds=61)
    assert formats(client, headers, file_id).status_code == 404


def test_formats_fail_closed_on_parser_drift(tmp_path):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    client = client_for(service)
    file_id = prepared_with(client, headers)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(xlsx_intake, "parser_fingerprint", lambda *args: "sha256:" + "1" * 64)
    try:
        response = formats(client, headers, file_id)
        assert response.status_code == 409
        assert response.json()["detail"] == "xlsx_parser_or_limits_changed_restage_required"
    finally:
        monkeypatch.undo()


def test_formats_deny_custom_codes_that_dlp_flags(tmp_path):
    class BlockingScanner:
        def check(self, content: bytes) -> None:
            if b"SECRET" in content:
                raise DLPDeniedError("dlp_personal_data")

    raw = package(
        entries_for(
            sheet(
                row(1, text_cell("A1", "name")),
                row(2, styled(text_cell("A2", "alpha"), 1)),
            ),
            extra=[("xl/styles.xml", styles_xml((164, '0.00"SECRET"'), xfs=(0, 164)).encode())],
        )
    )
    service = build_service(tmp_path, scanner=BlockingScanner())
    _, headers = credentials(service.broker)
    client = client_for(service)
    staged = stage(client, headers, raw, key="xlsx-formats-dlp").json()
    file_id = staged["file"]["file_id"]
    confirmed = confirm(client, headers, file_id, confirm_body(staged)).json()
    declared = declare(
        client,
        headers,
        file_id,
        declaration_body(confirmed, columns=[{"index": 0, "header": "name", "type": "text"}]),
    )
    assert declared.status_code == 200, declared.text
    response = formats(client, headers, file_id)
    assert response.status_code == 404
    assert response.json()["detail"] == "xlsx_file_unavailable"


def test_formats_enforce_style_and_code_budgets(tmp_path, monkeypatch):
    service = build_service(tmp_path)
    _, headers = credentials(service.broker)
    client = client_for(service)
    file_id = prepared_with(client, headers)
    monkeypatch.setattr(xlsx_formats, "MAX_FORMAT_CELLS", 2)
    response = formats(client, headers, file_id)
    assert response.status_code == 413
    assert response.json()["detail"] == "xlsx_formats_cell_limit"
    monkeypatch.setattr(xlsx_formats, "MAX_FORMAT_CELLS", 250_000)
    monkeypatch.setattr(xlsx_formats, "MAX_FORMAT_CODE_CHARS", 3)
    response = formats(client, headers, file_id)
    assert response.status_code == 413
    assert response.json()["detail"] == "xlsx_formats_code_limit"


def test_formats_reject_style_tables_that_cannot_be_bound():
    small = preview(
        sheet(row(1, text_cell("A1", "name")), row(2, styled(text_cell("A2", "alpha"), 4)))
    ).data
    interpretation = declared_intent(small, [{"index": 0, "header": "name", "type": "text"}])
    with pytest.raises(ValueError, match="xlsx_formats_style_invalid"):
        read_declared_formats(
            package(
                entries_for(
                    sheet(
                        row(1, text_cell("A1", "name")),
                        row(2, styled(text_cell("A2", "alpha"), 4)),
                    ),
                    extra=[("xl/styles.xml", styles_xml(xfs=(0, 9, 0)).encode())],
                )
            ),
            preview=small,
            interpretation=interpretation,
            check=lambda: None,
        )
    with pytest.raises(ValueError, match="xlsx_formats_styles_invalid"):
        read_declared_formats(
            package(
                entries_for(
                    sheet(row(1, text_cell("A1", "name")), row(2, text_cell("A2", "alpha"))),
                    extra=[("xl/styles.xml", b"<not-a-stylesheet/>")],
                )
            ),
            preview=small,
            interpretation=interpretation,
            check=lambda: None,
        )
    with pytest.raises(XlsxIntakeLimit, match="xlsx_formats_code_limit"):
        read_declared_formats(
            package(
                entries_for(
                    sheet(row(1, text_cell("A1", "name")), row(2, text_cell("A2", "alpha"))),
                    extra=[("xl/styles.xml", styles_xml((164, "x" * 300), xfs=(0, 164)).encode())],
                )
            ),
            preview=small,
            interpretation=interpretation,
            check=lambda: None,
        )


def test_format_contracts_hold_invariants():
    with pytest.raises(ValueError, match="xlsx_format_code_invalid"):
        XlsxFormatCode(num_fmt_id=164, kind="custom", code=None, cells=1)
    with pytest.raises(ValueError, match="xlsx_format_code_invalid"):
        XlsxFormatCode(num_fmt_id=0, kind="builtin", code="0.00", cells=1)
    with pytest.raises(ValueError, match="xlsx_formats_counts_invalid"):
        XlsxColumnFormats(
            index=0,
            header="name",
            type="text",
            cells=2,
            formats=(XlsxFormatCode(num_fmt_id=0, kind="builtin", cells=1),),
        )
    with pytest.raises(ValueError, match="xlsx_formats_order_invalid"):
        XlsxColumnFormats(
            index=0,
            header="name",
            type="text",
            cells=2,
            formats=(
                XlsxFormatCode(num_fmt_id=9, kind="builtin", cells=1),
                XlsxFormatCode(num_fmt_id=0, kind="builtin", cells=1),
            ),
        )
    report = XlsxFormatReport(
        interpretation_hash="sha256:" + "0" * 64,
        header_row=1,
        columns=(
            XlsxColumnFormats(
                index=0,
                header="name",
                type="text",
                cells=1,
                formats=(XlsxFormatCode(num_fmt_id=0, kind="builtin", cells=1),),
            ),
        ),
    )
    assert report.display_format == "inert-not-applied"
