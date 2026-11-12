from random import Random
from string import ascii_letters, digits
from xml.sax.saxutils import escape

import pytest
from pydantic import ValidationError
from test_stage13_xlsx_preflight import (
    CONTENT_TYPES,
    WORKSHEET,
    package,
    relationships_xml,
    valid_entries,
    workbook_xml,
)

from generaldataagent.datasets import xlsx_preview
from generaldataagent.datasets.xlsx_preflight import MAIN, XlsxPreflightError
from generaldataagent.datasets.xlsx_preview import (
    PREVIEW_COLUMNS,
    XlsxFormulaCell,
    XlsxPreviewData,
    XlsxPreviewError,
    XlsxPreviewResult,
    preview_xlsx,
)


def sheet(*rows):
    return f'<worksheet xmlns="{MAIN}"><sheetData>{"".join(rows)}</sheetData></worksheet>'


def row(number, *cells):
    return f'<row r="{number}">{"".join(cells)}</row>'


def text_cell(reference, value):
    return f'<c r="{reference}" t="str"><v>{escape(value)}</v></c>'


def number_cell(reference, value):
    return f'<c r="{reference}"><v>{value}</v></c>'


def shared_cell(reference, index):
    return f'<c r="{reference}" t="s"><v>{index}</v></c>'


def inline_cell(reference, value):
    return f'<c r="{reference}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'


def formula_cell(reference, formula, cached=None, kind=None):
    attributes = "" if kind is None else f' t="{kind}"'
    cached_xml = "" if cached is None else f"<v>{escape(str(cached))}</v>"
    return f'<c r="{reference}"{attributes}><f>{escape(formula)}</f>{cached_xml}</c>'


def large_text(size=32_769):
    return "".join(Random(size).choices(ascii_letters + digits, k=size))


def sst(*values):
    items = "".join(f"<si><t>{escape(value)}</t></si>" for value in values)
    return f'<sst xmlns="{MAIN}">{items}</sst>'


def entries_for(worksheet, *, shared=None, workbook=None, relationships=None, extra=()):
    entries = valid_entries()
    entries[3] = ("xl/worksheets/sheet1.xml", worksheet.encode())
    if shared is not None:
        entries[4] = ("xl/sharedStrings.xml", shared.encode())
    else:
        entries = [entry for entry in entries if entry[0] != "xl/sharedStrings.xml"]
    if workbook is not None:
        entries[1] = ("xl/workbook.xml", workbook.encode())
    if relationships is not None:
        entries[2] = ("xl/_rels/workbook.xml.rels", relationships.encode())
    return entries + list(extra)


def preview(worksheet, *, shared=None, workbook=None, relationships=None, sheet_name=None):
    entries = entries_for(worksheet, shared=shared, workbook=workbook, relationships=relationships)
    return preview_xlsx(package(entries), sheet=sheet_name)


def column_letters(column):
    letters = ""
    while column:
        column, remainder = divmod(column - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def reject(worksheet, code, *, shared=None, workbook=None, relationships=None):
    with pytest.raises(XlsxPreviewError) as caught:
        preview(worksheet, shared=shared, workbook=workbook, relationships=relationships)
    assert caught.value.code == code
    assert str(caught.value) == code


def test_text_preview_keeps_raw_strings_and_reports_sparse_header():
    result = preview(
        sheet(
            row(
                1,
                shared_cell("A1", 0),
                shared_cell("B1", 1),
                shared_cell("C1", 2),
            ),
            row(
                2,
                text_cell("A2", "x"),
                number_cell("B2", "10"),
                '<c r="C2"/>',
                text_cell("D2", "beta"),
            ),
            row(3, inline_cell("A3", "y"), number_cell("B3", "2.50")),
        ),
        shared=sst("name", "amount", "note", "beta"),
    )
    assert result.data.sheet == "Data"
    assert result.data.header_row == 1
    assert result.data.row_count == 3
    assert result.data.column_count == 4
    assert result.data.header == ("name", "amount", "note", "")
    assert result.data.rows == (
        ("x", "10", "", "beta"),
        ("y", "2.50", "", ""),
    )
    assert result.data.empty_headers == (3,)
    assert result.data.duplicate_headers == ()
    assert result.data.columns_truncated is False
    assert result.data.rows_truncated is False
    assert result.data.values == "inert-text-no-conversion"
    assert result.data.evaluation == "never"
    assert result.data.dates == "serial-text-not-converted"
    assert result.preflight.content_sha256.startswith("sha256:")
    assert result.preflight.sheets[0].cell_count == 9


def test_formula_text_and_cached_value_are_separate_and_never_evaluated():
    result = preview(
        sheet(
            row(1, shared_cell("A1", 0), shared_cell("B1", 1)),
            row(
                2,
                text_cell("A2", "x"),
                formula_cell("B2", 'A2&""', "x", kind="str"),
                formula_cell("C2", "1+1"),
            ),
        ),
        shared=sst("label", "value"),
    )
    assert result.data.header == ("label", "value", "")
    assert result.data.rows == (("x", "x", ""),)
    assert result.data.formulas == (
        XlsxFormulaCell(reference="B2", formula='A2&""', cached_text="x"),
        XlsxFormulaCell(reference="C2", formula="1+1", cached_text=None),
    )
    fact = result.preflight.sheets[0]
    assert (fact.formula_cells, fact.cached_formula_values, fact.formulas_without_cached_value) == (
        2,
        1,
        1,
    )


def test_boolean_error_and_string_cells_stay_raw_text():
    result = preview(
        sheet(
            row(1, text_cell("A1", "kind"), text_cell("B1", "value")),
            row(
                2,
                '<c r="A2" t="b"><v>1</v></c>',
                '<c r="B2" t="e"><v>#N/A</v></c>',
            ),
            row(3, '<c r="A3" t="str"><v>literal</v></c>', number_cell("B3", "007")),
        )
    )
    assert result.data.rows == (("1", "#N/A"), ("literal", "007"))


def test_rich_text_shared_string_and_formula_with_shared_cached_value():
    rich = (
        f'<sst xmlns="{MAIN}"><si><r><t>al</t></r><r><t>pha</t></r></si><si><t>beta</t></si></sst>'
    )
    result = preview(
        sheet(
            row(1, shared_cell("A1", 0), shared_cell("B1", 1)),
            row(2, shared_cell("A2", 1), formula_cell("B2", "A2", 0, kind="s")),
        ),
        shared=rich,
    )
    assert result.data.header == ("alpha", "beta")
    assert result.data.rows == (("beta", "alpha"),)
    assert result.data.formulas == (
        XlsxFormulaCell(reference="B2", formula="A2", cached_text="alpha"),
    )


def test_sheet_selection_and_unknown_sheet():
    first = sheet(row(1, text_cell("A1", "first")))
    second = sheet(row(1, text_cell("A1", "second")))
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
        ("xl/worksheets/sheet1.xml", first),
        ("xl/worksheets/sheet2.xml", second),
    ]
    data = package(entries)
    assert preview_xlsx(data).data.header == ("first",)
    assert preview_xlsx(data, sheet="Second").data.header == ("second",)
    with pytest.raises(XlsxPreviewError) as caught:
        preview_xlsx(data, sheet="Missing")
    assert caught.value.code == "xlsx_preview_sheet_not_found"
    with pytest.raises(XlsxPreviewError):
        preview_xlsx(data, sheet=1)


def test_header_row_need_not_start_at_one():
    result = preview(sheet(row(5, text_cell("A5", "head")), row(6, text_cell("A6", "value"))))
    assert result.data.header_row == 5
    assert result.data.row_count == 2
    assert result.data.rows == (("value",),)


def test_cells_without_references_are_positioned_sequentially():
    result = preview(
        sheet(
            row(1, "<c><v>1</v></c>", '<c r="C1"><v>3</v></c>'),
            row(2, "<c><v>4</v></c>"),
        )
    )
    assert result.data.header == ("1", "", "3")
    assert result.data.rows == (("4", "", ""),)


def test_preview_truncates_rows_and_columns_explicitly():
    header = "".join(number_cell(f"{column_letters(index)}1", index) for index in range(1, 71))
    body = "".join(
        row(
            number,
            *(number_cell(f"{column_letters(index)}{number}", index) for index in range(1, 71)),
        )
        for number in range(2, 9)
    )
    result = preview(sheet(row(1, *[header]), body))
    assert result.data.row_count == 8
    assert result.data.column_count == 70
    assert len(result.data.header) == PREVIEW_COLUMNS
    assert len(result.data.rows) == 5
    assert result.data.columns_truncated is True
    assert result.data.rows_truncated is True
    assert result.data.header[:2] == ("1", "2")


def test_header_only_sheet_has_no_sample_rows():
    result = preview(sheet(row(1, text_cell("A1", "head"))))
    assert result.data.rows == ()
    assert result.data.rows_truncated is False


def test_empty_sheet_is_rejected():
    reject(sheet(), "xlsx_preview_empty_sheet")


def test_row_without_cells_is_rejected():
    reject(sheet(row(1)), "xlsx_preview_empty_sheet")


def test_empty_and_duplicate_headers_are_reported():
    result = preview(
        sheet(
            row(1, text_cell("A1", "a"), '<c r="B1"/>', text_cell("C1", "a"), '<c r="D1"/>'),
            row(2, text_cell("A2", "1")),
        )
    )
    assert result.data.empty_headers == (1, 3)
    assert result.data.duplicate_headers == ("a",)


def test_missing_shared_strings_part_without_shared_cells():
    result = preview(sheet(row(1, number_cell("A1", "1"))))
    assert result.data.header == ("1",)
    assert result.preflight.shared_strings == 0


@pytest.mark.parametrize("raw", ["9", "x", "-1", ""])
def test_invalid_shared_string_index_is_rejected(raw):
    cell = f'<c r="A1" t="s"><v>{raw}</v></c>'
    reject(sheet(row(1, cell)), "xlsx_preview_shared_string_invalid", shared=sst("only"))


def test_shared_string_cell_too_large():
    reject(
        sheet(row(1, shared_cell("A1", 0))),
        "xlsx_preview_cell_too_large",
        shared=sst(large_text()),
    )


def test_formula_text_too_large():
    reject(
        sheet(row(1, formula_cell("A1", large_text()))),
        "xlsx_preview_cell_too_large",
    )


def test_cached_formula_value_too_large():
    reject(
        sheet(row(1, formula_cell("A1", "1+1", large_text()))),
        "xlsx_preview_cell_too_large",
    )


def test_plain_cell_too_large():
    reject(
        sheet(row(1, f'<c r="A1"><v>{large_text()}</v></c>')),
        "xlsx_preview_cell_too_large",
    )


def test_total_preview_text_limit(monkeypatch):
    monkeypatch.setattr(xlsx_preview, "MAX_PREVIEW_CHARS", 4)
    reject(sheet(row(1, text_cell("A1", "abcde"))), "xlsx_preview_too_large")


@pytest.mark.parametrize(
    "worksheet",
    [
        f'<worksheet xmlns="{MAIN}"><sheetData><row r="2"><c r="A1"><v>1</v></c></row>'
        "</sheetData></worksheet>",
        f'<worksheet xmlns="{MAIN}"><sheetData><row r="0"><c r="A1"><v>1</v></c></row>'
        "</sheetData></worksheet>",
        f'<worksheet xmlns="{MAIN}"><sheetData><row r="1"><c r="A100001"><v>1</v></c></row>'
        "</sheetData></worksheet>",
        f'<worksheet xmlns="{MAIN}"><sheetData><row r="1"><c r="A1"><v>1</v></c></row>'
        '<row r="1"><c r="A1"><v>1</v></c></row></sheetData></worksheet>',
    ],
)
def test_invalid_sheet_structure_is_rejected(worksheet):
    reject(worksheet, "xlsx_preview_sheet_invalid")


@pytest.mark.parametrize(
    "worksheet",
    [
        f'<other xmlns="{MAIN}"/>',
        f'<worksheet xmlns="{MAIN}"><sheetData><row r="1"><c r="1A"><v>1</v></c></row>'
        "</sheetData></worksheet>",
        f'<worksheet xmlns="{MAIN}">',
    ],
)
def test_preflight_structural_rejection_propagates(worksheet):
    with pytest.raises(XlsxPreflightError) as caught:
        preview(worksheet)
    assert caught.value.code == "xlsx_preflight_sheet_invalid"


def test_preflight_rejection_propagates():
    entries = entries_for(sheet(row(1, number_cell("A1", "1"))))
    entries.append(("xl/vbaProject.bin", b"<x/>"))
    with pytest.raises(XlsxPreflightError) as caught:
        preview_xlsx(package(entries))
    assert caught.value.code == "xlsx_preflight_macro_not_allowed"


def test_preview_data_rejects_inconsistent_widths_and_flags():
    with pytest.raises(ValidationError):
        XlsxPreviewData(
            sheet="Data",
            header_row=1,
            row_count=2,
            row_numbers=(1, 2),
            column_count=2,
            header=("a", "b"),
            rows=(("1",),),
            formulas=(),
            empty_headers=(),
            duplicate_headers=(),
            columns_truncated=False,
            rows_truncated=False,
        )
    with pytest.raises(ValidationError):
        XlsxPreviewData(
            sheet="Data",
            header_row=1,
            row_count=2,
            row_numbers=(1, 2),
            column_count=2,
            header=("a",),
            rows=(("1",),),
            formulas=(),
            empty_headers=(),
            duplicate_headers=(),
            columns_truncated=False,
            rows_truncated=True,
        )
    with pytest.raises(ValidationError):
        XlsxPreviewData(
            sheet="Data",
            header_row=1,
            row_count=1,
            row_numbers=(1,),
            column_count=1,
            header=("a",),
            rows=(),
            formulas=(),
            empty_headers=(1,),
            duplicate_headers=(),
            columns_truncated=False,
            rows_truncated=False,
        )
    with pytest.raises(ValidationError):
        XlsxPreviewData(
            sheet="Data",
            header_row=1,
            row_count=1,
            row_numbers=(1,),
            column_count=1,
            header=("a", "a"),
            rows=(),
            formulas=(),
            empty_headers=(),
            duplicate_headers=("a", "a"),
            columns_truncated=False,
            rows_truncated=False,
        )


def test_preview_result_requires_a_preflighted_sheet():
    result = preview(sheet(row(1, text_cell("A1", "head"))))
    payload = result.model_dump(mode="json")
    payload["data"]["sheet"] = "Missing"
    with pytest.raises(ValidationError):
        XlsxPreviewResult.model_validate(payload)


def test_preview_row_numbers_cover_exactly_the_materialized_rows():
    result = preview(sheet(row(1, text_cell("A1", "head")), row(3, text_cell("A3", "x"))))
    assert result.data.row_numbers == (1, 3)
    payload = result.data.model_dump(mode="json")
    for numbers in ((), (3,), (2, 3), (1, 1)):
        with pytest.raises(ValidationError):
            XlsxPreviewData.model_validate(payload | {"row_numbers": numbers})
