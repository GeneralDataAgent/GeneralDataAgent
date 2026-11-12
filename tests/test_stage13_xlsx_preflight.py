import io
import zipfile

import pytest
from pydantic import ValidationError

from generaldataagent.datasets import xlsx_preflight
from generaldataagent.datasets.xlsx_preflight import (
    DOC_REL,
    MAIN,
    PKG_REL,
    XlsxPreflightError,
    XlsxSheetPreflight,
    preflight_xlsx,
)

CONTENT_TYPES = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
)
RELATIONSHIPS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    f'<Relationships xmlns="{PKG_REL}">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
    'Target="worksheets/sheet1.xml"/>'
    "</Relationships>"
).encode()
WORKBOOK = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    f'<workbook xmlns="{MAIN}" xmlns:r="{DOC_REL}">'
    '<sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets>'
    "</workbook>"
).encode()
SHEET = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    f'<worksheet xmlns="{MAIN}"><sheetData>'
    '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><v>2</v></c>'
    '<c r="C1"><f>SUM(A1:B1)</f><v>3</v></c><c r="D1"><f>1+1</f></c></row>'
    '<row r="2"><c r="A2"><v>3</v></c></row>'
    "</sheetData></worksheet>"
).encode()
SHARED_STRINGS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    f'<sst xmlns="{MAIN}" count="2" uniqueCount="2">'
    "<si><t>alpha</t></si><si><t>beta</t></si></sst>"
).encode()


def package(entries, *, compression=zipfile.ZIP_DEFLATED):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return stream.getvalue()


def valid_entries():
    return [
        ("[Content_Types].xml", CONTENT_TYPES),
        ("xl/workbook.xml", WORKBOOK),
        ("xl/_rels/workbook.xml.rels", RELATIONSHIPS),
        ("xl/worksheets/sheet1.xml", SHEET),
        ("xl/sharedStrings.xml", SHARED_STRINGS),
    ]


def with_entries(*extra):
    return valid_entries() + list(extra)


def reject(data, code):
    with pytest.raises(XlsxPreflightError) as caught:
        preflight_xlsx(data)
    assert caught.value.code == code
    assert str(caught.value) == code


def workbook_xml(*sheets, date_system=None):
    properties = "" if date_system is None else f'<workbookPr date1904="{date_system}"/>'
    body = "".join(
        f'<sheet name="{name}" sheetId="{index}" r:id="{relation}"/>'
        for index, (name, relation) in enumerate(sheets, start=1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<workbook xmlns="{MAIN}" xmlns:r="{DOC_REL}">{properties}'
        f"<sheets>{body}</sheets></workbook>"
    ).encode()


def relationships_xml(rows):
    body = "".join(
        f'<Relationship Id="{relation_id}" Type="{kind}" Target="{target}"{extra}/>'
        for relation_id, kind, target, extra in rows
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{PKG_REL}">{body}'
        "</Relationships>"
    ).encode()


WORKSHEET = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
CHART_SHEET = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/chartsheet"


def test_structure_only_report_counts_rows_cells_and_formula_caches():
    report = preflight_xlsx(package(valid_entries()))
    assert report.inspector == "gda-xlsx-preflight-v1"
    assert report.content_sha256.startswith("sha256:")
    assert report.zip_entries == 5
    assert report.shared_strings == 2
    assert report.date_system == "1900"
    assert report.sheets == (
        XlsxSheetPreflight(
            name="Data",
            row_count=2,
            cell_count=5,
            formula_cells=2,
            cached_formula_values=1,
            formulas_without_cached_value=1,
        ),
    )
    assert report.cell_values == "not-extracted"
    assert report.formula_values == "inert-formula-text-not-evaluated"
    assert (
        report.macro_parts == report.external_link_parts == report.embedded_object_parts == "none"
    )


def test_1904_date_system_and_absent_shared_strings():
    entries = valid_entries()
    entries[1] = ("xl/workbook.xml", workbook_xml(("Data", "rId1"), date_system="1"))
    entries.pop()  # no sharedStrings part
    report = preflight_xlsx(package(entries))
    assert report.date_system == "1904"
    assert report.shared_strings == 0


def test_explicit_false_date_flag_uses_1900():
    entries = valid_entries()
    entries[1] = ("xl/workbook.xml", workbook_xml(("Data", "rId1"), date_system="false"))
    assert preflight_xlsx(package(entries)).date_system == "1900"


def test_absolute_sheet_target_is_resolved_inside_the_package():
    entries = valid_entries()
    entries[2] = (
        "xl/_rels/workbook.xml.rels",
        relationships_xml([("rId1", WORKSHEET, "/xl/worksheets/sheet1.xml", "")]),
    )
    assert preflight_xlsx(package(entries)).sheets[0].cell_count == 5


def test_sheet_without_rows_reports_zeros():
    entries = valid_entries()
    entries[3] = (
        "xl/worksheets/sheet1.xml",
        f'<worksheet xmlns="{MAIN}"><sheetData/></worksheet>'.encode(),
    )
    sheet = preflight_xlsx(package(entries)).sheets[0]
    assert (sheet.row_count, sheet.cell_count, sheet.formula_cells) == (0, 0, 0)


def test_cell_without_reference_is_counted():
    entries = valid_entries()
    entries[3] = (
        "xl/worksheets/sheet1.xml",
        f'<worksheet xmlns="{MAIN}"><sheetData><row><c><v>1</v></c>'
        "</row></sheetData></worksheet>".encode(),
    )
    assert preflight_xlsx(package(entries)).sheets[0].cell_count == 1


@pytest.mark.parametrize("data", [b"", b"not a zip", b"PK\x03\x04"])
def test_empty_and_non_zip_inputs_are_rejected(data):
    reject(data, "xlsx_preflight_empty" if not data else "xlsx_preflight_not_zip")


def test_input_byte_limit(monkeypatch):
    monkeypatch.setattr(xlsx_preflight, "MAX_BYTES", 10)
    reject(package(valid_entries()), "xlsx_preflight_too_large")


def test_entry_count_limit(monkeypatch):
    monkeypatch.setattr(xlsx_preflight, "MAX_ENTRIES", 4)
    reject(package(valid_entries()), "xlsx_preflight_entry_limit")


def test_single_entry_byte_limit(monkeypatch):
    monkeypatch.setattr(xlsx_preflight, "MAX_ENTRY_BYTES", 1)
    reject(package(valid_entries()), "xlsx_preflight_entry_too_large")


def test_declared_expansion_ratio_limit(monkeypatch):
    monkeypatch.setattr(xlsx_preflight, "MAX_COMPRESSION_RATIO", 1)
    reject(package(with_entries(("xl/filler.xml", b" " * 4096))), "xlsx_preflight_expansion_limit")


def test_total_uncompressed_limit(monkeypatch):
    monkeypatch.setattr(xlsx_preflight, "MAX_TOTAL_UNCOMPRESSED", 1)
    reject(package(valid_entries()), "xlsx_preflight_expansion_limit")


@pytest.mark.parametrize(
    "name", ["xl/../evil.xml", "/evil.xml", "xl\\evil.xml", "xl//evil.xml", "./evil.xml"]
)
def test_unsafe_entry_names_are_rejected(name):
    reject(package(with_entries((name, b"<x/>"))), "xlsx_preflight_unsafe_entry")


def test_duplicate_entry_names_are_rejected():
    with pytest.warns(UserWarning):
        data = package(with_entries(("xl/workbook.xml", WORKBOOK)))
    reject(data, "xlsx_preflight_unsafe_entry")


def test_encrypted_entry_flag_is_rejected():
    data = bytearray(package(valid_entries()))
    local = data.index(b"PK\x03\x04")
    data[local + 6 : local + 8] = b"\x01\x00"
    central = data.index(b"PK\x01\x02")
    data[central + 8 : central + 10] = b"\x01\x00"
    reject(bytes(data), "xlsx_preflight_encrypted")


@pytest.mark.parametrize(
    "name",
    [
        "xl/vbaProject.bin",
        "xl/externalLinks/externalLink1.xml",
        "xl/connections.xml",
        "xl/querytables/queryTable1.xml",
        "xl/embeddings/oleObject1.bin",
    ],
)
def test_active_content_parts_are_rejected(name):
    expected = {
        "xl/vbaProject.bin": "xlsx_preflight_macro_not_allowed",
        "xl/externalLinks/externalLink1.xml": "xlsx_preflight_external_links_not_allowed",
        "xl/connections.xml": "xlsx_preflight_external_links_not_allowed",
        "xl/querytables/queryTable1.xml": "xlsx_preflight_external_links_not_allowed",
        "xl/embeddings/oleObject1.bin": "xlsx_preflight_embedded_objects_not_allowed",
    }[name]
    reject(package(with_entries((name, b"<x/>"))), expected)


def test_macro_enabled_content_type_is_rejected():
    content_types = (
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        b'<Override PartName="/xl/workbook.xml" '
        b'ContentType="application/vnd.ms-excel.sheet.macroEnabled.main+xml"/>'
        b"</Types>"
    )
    entries = valid_entries()
    entries[0] = ("[Content_Types].xml", content_types)
    reject(package(entries), "xlsx_preflight_macro_not_allowed")


@pytest.mark.parametrize("missing", ["[Content_Types].xml", "xl/workbook.xml"])
def test_required_parts_are_required(missing):
    entries = [entry for entry in valid_entries() if entry[0] != missing]
    reject(package(entries), "xlsx_preflight_package_invalid")


def test_missing_relationship_part_is_required():
    entries = [entry for entry in valid_entries() if not entry[0].endswith("workbook.xml.rels")]
    reject(package(entries), "xlsx_preflight_package_invalid")


def test_relationship_without_target_is_rejected():
    entries = valid_entries()
    entries[2] = (
        "xl/_rels/workbook.xml.rels",
        f'<?xml version="1.0"?><Relationships xmlns="{PKG_REL}">'
        f'<Relationship Id="rId1" Type="{WORKSHEET}"/></Relationships>'.encode(),
    )
    reject(package(entries), "xlsx_preflight_package_invalid")


def test_external_relationship_mode_is_rejected():
    entries = valid_entries()
    entries[2] = (
        "xl/_rels/workbook.xml.rels",
        relationships_xml(
            [
                ("rId1", WORKSHEET, "worksheets/sheet1.xml", ""),
                (
                    "rId2",
                    "http://example.test/hyperlink",
                    "https://example.test",
                    ' TargetMode="External"',
                ),
            ]
        ),
    )
    reject(package(entries), "xlsx_preflight_external_links_not_allowed")


def test_sheet_relationship_must_resolve_to_an_existing_worksheet():
    entries = valid_entries()
    entries[2] = (
        "xl/_rels/workbook.xml.rels",
        relationships_xml([("rId1", WORKSHEET, "../worksheets/sheet1.xml", "")]),
    )
    reject(package(entries), "xlsx_preflight_sheet_invalid")


def test_chartsheet_is_not_a_worksheet():
    entries = valid_entries()
    entries[2] = (
        "xl/_rels/workbook.xml.rels",
        relationships_xml([("rId1", CHART_SHEET, "worksheets/sheet1.xml", "")]),
    )
    reject(package(entries), "xlsx_preflight_sheet_invalid")


@pytest.mark.parametrize(
    "book",
    [
        workbook_xml(("Data", "missing")),
        workbook_xml(("", "rId1")),
        workbook_xml(("x" * 32, "rId1")),
        workbook_xml(("Data", "rId1"), ("Data", "rId1")),
        b'<workbook xmlns="' + MAIN.encode() + b'"><sheets></sheets></workbook>',
    ],
)
def test_invalid_sheet_declarations_are_rejected(book):
    entries = valid_entries()
    entries[1] = ("xl/workbook.xml", book)
    code = (
        "xlsx_preflight_sheet_limit"
        if b"<sheets></sheets>" in book
        else "xlsx_preflight_sheet_invalid"
    )
    reject(package(entries), code)


def test_sheet_count_limit(monkeypatch):
    monkeypatch.setattr(xlsx_preflight, "MAX_SHEETS", 1)
    entries = valid_entries()
    entries[1] = ("xl/workbook.xml", workbook_xml(("Data", "rId1"), ("More", "rId1")))
    reject(package(entries), "xlsx_preflight_sheet_limit")


@pytest.mark.parametrize("book", [b"<workbook>", b'<workbook xmlns="' + MAIN.encode() + b'">'])
def test_malformed_workbook_is_rejected(book):
    entries = valid_entries()
    entries[1] = ("xl/workbook.xml", book)
    reject(package(entries), "xlsx_preflight_workbook_invalid")


def test_malformed_relationships_are_rejected():
    entries = valid_entries()
    entries[2] = ("xl/_rels/workbook.xml.rels", b"<Relationships>")
    reject(package(entries), "xlsx_preflight_package_invalid")


def test_malformed_content_types_are_rejected():
    entries = valid_entries()
    entries[0] = ("[Content_Types].xml", b"<Types>")
    reject(package(entries), "xlsx_preflight_package_invalid")


def test_malformed_shared_strings_are_rejected():
    entries = valid_entries()
    entries[4] = ("xl/sharedStrings.xml", b"<sst>")
    reject(package(entries), "xlsx_preflight_shared_strings_invalid")


@pytest.mark.parametrize(
    ("target", "root", "code"),
    [
        (0, b"<NotTypes/>", "xlsx_preflight_package_invalid"),
        (1, f'<other xmlns="{MAIN}"/>'.encode(), "xlsx_preflight_workbook_invalid"),
        (2, f'<other xmlns="{PKG_REL}"/>'.encode(), "xlsx_preflight_package_invalid"),
        (3, f'<other xmlns="{MAIN}"/>'.encode(), "xlsx_preflight_sheet_invalid"),
        (4, f'<other xmlns="{MAIN}"/>'.encode(), "xlsx_preflight_shared_strings_invalid"),
    ],
)
def test_unexpected_root_elements_are_rejected(target, root, code):
    entries = valid_entries()
    entries[target] = (entries[target][0], root)
    reject(package(entries), code)


@pytest.mark.parametrize("target", [0, 1, 2, 3, 4])
def test_xml_declarations_are_rejected_in_every_parsed_part(target):
    entries = valid_entries()
    injected = b'<!DOCTYPE x [<!ENTITY y "z">]><x/>'
    entries[target] = (entries[target][0], injected)
    reject(package(entries), "xlsx_preflight_xml_declaration_not_allowed")


def test_invalid_date_system_value_is_rejected():
    entries = valid_entries()
    entries[1] = ("xl/workbook.xml", workbook_xml(("Data", "rId1"), date_system="maybe"))
    reject(package(entries), "xlsx_preflight_workbook_invalid")


def test_shared_strings_limit(monkeypatch):
    monkeypatch.setattr(xlsx_preflight, "MAX_SHARED_STRINGS", 1)
    reject(package(valid_entries()), "xlsx_preflight_shared_strings_limit")


def cell_xml(reference):
    return (
        f'<worksheet xmlns="{MAIN}"><sheetData><row>'
        f'<c r="{reference}"><v>1</v></c></row></sheetData></worksheet>'
    ).encode()


@pytest.mark.parametrize("reference", ["1A", "A", "Ä1", "A1B"])
def test_malformed_cell_references_are_rejected(reference):
    entries = valid_entries()
    entries[3] = ("xl/worksheets/sheet1.xml", cell_xml(reference))
    reject(package(entries), "xlsx_preflight_sheet_invalid")


def test_row_limit(monkeypatch):
    monkeypatch.setattr(xlsx_preflight, "MAX_ROWS", 1)
    reject(package(valid_entries()), "xlsx_preflight_dimension_limit")


def test_column_limit(monkeypatch):
    monkeypatch.setattr(xlsx_preflight, "MAX_COLUMNS", 2)
    reject(package(valid_entries()), "xlsx_preflight_dimension_limit")


def test_total_cell_limit(monkeypatch):
    monkeypatch.setattr(xlsx_preflight, "MAX_CELLS", 4)
    reject(package(valid_entries()), "xlsx_preflight_dimension_limit")


def test_total_cell_limit_counts_across_sheets(monkeypatch):
    monkeypatch.setattr(xlsx_preflight, "MAX_CELLS", 6)
    sheet = (
        f'<worksheet xmlns="{MAIN}"><sheetData><row>'
        '<c r="A1"/><c r="B1"/><c r="C1"/><c r="D1"/></row></sheetData></worksheet>'
    ).encode()
    entries = valid_entries()
    entries[1] = ("xl/workbook.xml", workbook_xml(("Data", "rId1"), ("More", "rId2")))
    entries[2] = (
        "xl/_rels/workbook.xml.rels",
        relationships_xml(
            [
                ("rId1", WORKSHEET, "worksheets/sheet1.xml", ""),
                ("rId2", WORKSHEET, "worksheets/sheet2.xml", ""),
            ]
        ),
    )
    entries[3] = ("xl/worksheets/sheet1.xml", sheet)
    entries.append(("xl/worksheets/sheet2.xml", sheet))
    reject(package(entries), "xlsx_preflight_dimension_limit")


def test_formula_counts_must_add_up():
    with pytest.raises(ValidationError):
        XlsxSheetPreflight(
            name="Data",
            row_count=1,
            cell_count=1,
            formula_cells=1,
            cached_formula_values=0,
            formulas_without_cached_value=0,
        )
