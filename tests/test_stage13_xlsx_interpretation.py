import pytest
from pydantic import ValidationError
from test_stage13_xlsx_preview import (
    formula_cell,
    number_cell,
    preview,
    row,
    sheet,
    text_cell,
)

from generaldataagent.datasets.xlsx_intake_contracts import (
    XlsxColumnDeclaration,
    XlsxInterpretation,
    interpretation_digest,
)
from generaldataagent.datasets.xlsx_interpretation import (
    XlsxInterpretationResult,
    XlsxInterpretedCell,
    XlsxInterpretedColumn,
    interpret_preview,
)

ZERO = "sha256:" + "0" * 64


def bool_cell(reference, value):
    return f'<c r="{reference}" t="b"><v>{value}</v></c>'


def error_cell(reference, value):
    return f'<c r="{reference}" t="e"><v>{value}</v></c>'


def invalid_number_cell(reference, value):
    return f'<c r="{reference}"><v>{value}</v></c>'


def declare(preview, columns, *, header_row=None, duplicate_rows="reject"):
    declared = tuple(XlsxColumnDeclaration(**column) for column in columns)
    header_row = preview.header_row if header_row is None else header_row
    return XlsxInterpretation(
        source_hash=ZERO,
        preview_hash=ZERO,
        confirmation_hash=ZERO,
        header_row=header_row,
        columns=declared,
        duplicate_rows=duplicate_rows,
        interpretation_hash=interpretation_digest(
            source_hash=ZERO,
            preview_hash=ZERO,
            confirmation_hash=ZERO,
            header_row=header_row,
            columns=declared,
            duplicate_rows=duplicate_rows,
        ),
    )


def readings(result, index):
    return {
        cell.row: (cell.status, cell.value, cell.reason) for cell in result.columns[index].cells
    }


def test_declared_types_are_canonicalised_without_changing_the_raw_text():
    data = preview(
        sheet(
            row(
                1,
                text_cell("A1", "text"),
                text_cell("B1", "integer"),
                text_cell("C1", "decimal"),
                text_cell("D1", "flag"),
            ),
            row(
                2,
                text_cell("A2", "alpha"),
                number_cell("B2", "1.0"),
                number_cell("C2", "0010.500"),
                bool_cell("D2", "1"),
            ),
            row(
                3,
                text_cell("A3", "beta"),
                number_cell("B3", "1E+3"),
                number_cell("C3", "0.00"),
                bool_cell("D3", "0"),
            ),
        )
    ).data
    ordered = declare(
        data,
        [
            {"index": 0, "header": "text", "type": "text"},
            {"index": 1, "header": "integer", "type": "integer"},
            {"index": 2, "header": "decimal", "type": "decimal"},
            {"index": 3, "header": "flag", "type": "boolean"},
        ],
    )
    result = interpret_preview(data, ordered, date_system="1900")
    assert [column.type for column in result.columns] == ["text", "integer", "decimal", "boolean"]
    assert readings(result, 0) == {2: ("value", "alpha", None), 3: ("value", "beta", None)}
    assert readings(result, 1) == {2: ("value", "1", None), 3: ("value", "1000", None)}
    assert readings(result, 2) == {2: ("value", "10.5", None), 3: ("value", "0", None)}
    assert readings(result, 3) == {2: ("value", "true", None), 3: ("value", "false", None)}
    raw = {cell.row: cell.raw for cell in result.columns[2].cells}
    assert raw == {2: "0010.500", 3: "0.00"}
    assert result.scope == "materialized-preview-window"
    assert result.storage == "derived-not-persisted"
    assert result.formula_cached_values == "never-trusted"
    assert result.duplicate_rows == "not-evaluated"


def test_empty_error_formula_and_kind_conflicts_are_explicit():
    data = preview(
        sheet(
            row(
                1,
                text_cell("A1", "text"),
                text_cell("B1", "integer"),
                text_cell("C1", "flag"),
                text_cell("D1", "date"),
                text_cell("E1", "formula"),
            ),
            row(
                2,
                number_cell("A2", "5"),
                text_cell("B2", "x"),
                number_cell("C2", "1"),
                text_cell("D2", "x"),
                formula_cell("E2", "1+1", "10"),
            ),
            row(
                3,
                text_cell("A3", "NA"),
                number_cell("B3", "1.5"),
                bool_cell("C3", "true"),
                number_cell("D3", "1.5"),
                error_cell("E3", "#DIV/0!"),
            ),
            row(
                4,
                text_cell("A4", "na"),
                "",
                "",
                number_cell("D4", "0"),
                number_cell("E4", "9" * 65),
            ),
            row(5, "", invalid_number_cell("B5", "abc"), "", "", ""),
        )
    ).data
    ordered = declare(
        data,
        [
            {"index": 0, "header": "text", "type": "text", "missing_tokens": ["NA"]},
            {"index": 1, "header": "integer", "type": "integer", "empty_is_missing": False},
            {"index": 2, "header": "flag", "type": "boolean"},
            {"index": 3, "header": "date", "type": "date"},
            {"index": 4, "header": "formula", "type": "integer"},
        ],
    )
    result = interpret_preview(data, ordered, date_system="1900")
    assert readings(result, 0) == {
        2: ("unparsed", None, "kind_not_text"),
        3: ("missing", None, "missing_token"),
        4: ("value", "na", None),
        5: ("missing", None, "empty_cell"),
    }
    assert readings(result, 1) == {
        2: ("unparsed", None, "kind_not_numeric"),
        3: ("unparsed", None, "non_integral_number"),
        4: ("unparsed", None, "empty_not_missing"),
        5: ("unparsed", None, "invalid_number"),
    }
    assert readings(result, 2) == {
        2: ("unparsed", None, "kind_not_boolean"),
        3: ("unparsed", None, "invalid_boolean"),
        4: ("missing", None, "empty_cell"),
        5: ("missing", None, "empty_cell"),
    }
    assert readings(result, 3) == {
        2: ("unparsed", None, "kind_not_numeric"),
        3: ("unparsed", None, "fractional_serial"),
        4: ("unparsed", None, "out_of_range"),
        5: ("missing", None, "empty_cell"),
    }
    assert readings(result, 4) == {
        2: ("not_trusted", None, "formula_cached_value_not_trusted"),
        3: ("unparsed", None, "error_cell"),
        4: ("unparsed", None, "value_too_large"),
        5: ("missing", None, "empty_cell"),
    }


def test_date_serials_follow_the_1900_system_including_the_fictitious_leap_day():
    data = preview(
        sheet(
            row(1, text_cell("A1", "date")),
            row(2, number_cell("A2", "1")),
            row(3, number_cell("A3", "59")),
            row(4, number_cell("A4", "60")),
            row(5, number_cell("A5", "61")),
            row(6, number_cell("A6", "36526")),
        )
    ).data
    result = interpret_preview(
        data,
        declare(data, [{"index": 0, "header": "date", "type": "date"}]),
        date_system="1900",
    )
    assert readings(result, 0) == {
        2: ("value", "1900-01-01", None),
        3: ("value", "1900-02-28", None),
        4: ("unparsed", None, "invalid_serial"),
        5: ("value", "1900-03-01", None),
        6: ("value", "2000-01-01", None),
    }


def test_date_serials_bound_the_range_and_follow_the_1904_system():
    western = preview(
        sheet(
            row(1, text_cell("A1", "date")),
            row(2, number_cell("A2", "2958465")),
            row(3, number_cell("A3", "2958466")),
        )
    ).data
    ordered = declare(western, [{"index": 0, "header": "date", "type": "date"}])
    assert readings(interpret_preview(western, ordered, date_system="1900"), 0) == {
        2: ("value", "9999-12-31", None),
        3: ("unparsed", None, "out_of_range"),
    }
    mac = preview(
        sheet(
            row(1, text_cell("A1", "date")),
            row(2, number_cell("A2", "0")),
            row(3, number_cell("A3", "1")),
            row(4, number_cell("A4", "2957003")),
            row(5, number_cell("A5", "2957004")),
        )
    ).data
    mac_ordered = declare(mac, [{"index": 0, "header": "date", "type": "date"}])
    assert readings(interpret_preview(mac, mac_ordered, date_system="1904"), 0) == {
        2: ("value", "1904-01-01", None),
        3: ("value", "1904-01-02", None),
        4: ("value", "9999-12-31", None),
        5: ("unparsed", None, "out_of_range"),
    }
    with pytest.raises(ValueError, match="xlsx_date_system_invalid"):
        interpret_preview(western, ordered, date_system="1901")


def test_declared_header_row_shifts_the_interpreted_data_rows():
    data = preview(
        sheet(
            row(1, text_cell("A1", "v1")),
            row(2, text_cell("A2", "v2")),
            row(3, text_cell("A3", "v3")),
        )
    ).data
    ordered = declare(
        data,
        [{"index": 0, "header": "v2", "type": "text"}],
        header_row=2,
    )
    result = interpret_preview(data, ordered, date_system="1900")
    assert result.header_row == 2
    assert readings(result, 0) == {3: ("value", "v3", None)}


def test_interpretation_must_bind_to_the_preview_header_and_columns():
    data = preview(
        sheet(
            row(1, text_cell("A1", "head")),
            row(2, text_cell("A2", "value")),
        )
    ).data
    wrong_header = declare(data, [{"index": 0, "header": "other", "type": "text"}])
    with pytest.raises(ValueError, match="xlsx_interpretation_binding_invalid"):
        interpret_preview(data, wrong_header, date_system="1900")
    wrong_index = declare(data, [{"index": 1, "header": "head", "type": "text"}])
    with pytest.raises(ValueError, match="xlsx_interpretation_binding_invalid"):
        interpret_preview(data, wrong_index, date_system="1900")
    outside = declare(data, [{"index": 0, "header": "head", "type": "text"}], header_row=99)
    with pytest.raises(ValueError, match="xlsx_interpretation_binding_invalid"):
        interpret_preview(data, outside, date_system="1900")


def test_interpreted_cells_columns_and_results_enforce_consistency():
    with pytest.raises(ValidationError):
        XlsxInterpretedCell(row=2, raw="1", kind="number", status="value")
    with pytest.raises(ValidationError):
        XlsxInterpretedCell(
            row=2, raw="1", kind="number", status="value", value="1", reason="error_cell"
        )
    with pytest.raises(ValidationError):
        XlsxInterpretedCell(row=2, raw="1", kind="number", status="missing", reason="error_cell")
    with pytest.raises(ValidationError):
        XlsxInterpretedCell(
            row=2, raw="1", kind="number", status="unparsed", reason="missing_token"
        )
    good = XlsxInterpretedCell(row=2, raw="1", kind="number", status="value", value="1")
    with pytest.raises(ValidationError):
        XlsxInterpretedColumn(
            index=0, header="h", type="integer", cells=(good, good.model_copy(update={"row": 2}))
        )
    with pytest.raises(ValidationError):
        XlsxInterpretationResult(
            interpretation_hash=ZERO,
            header_row=1,
            date_system="1900",
            columns=(
                XlsxInterpretedColumn(index=0, header="a", type="text", cells=()),
                XlsxInterpretedColumn(index=0, header="b", type="text", cells=()),
            ),
        )
