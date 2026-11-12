"""Bounded, structure-only XLSX preflight; never extracts cell values or evaluates formulas."""

import hashlib
import io
import posixpath
import zipfile
from typing import Literal
from xml.etree import ElementTree

from pydantic import Field, model_validator

from generaldataagent.contracts.models import ContractModel

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DOC_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"

MAX_BYTES = 16 * 1024 * 1024
MAX_ENTRIES = 256
MAX_ENTRY_BYTES = 16 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED = 64 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
MAX_SHEETS = 64
MAX_ROWS = 100_000
MAX_COLUMNS = 1_024
MAX_CELLS = 1_000_000
MAX_SHARED_STRINGS = 200_000

REQUIRED_PARTS = (
    "[Content_Types].xml",
    "xl/workbook.xml",
    "xl/_rels/workbook.xml.rels",
)


class XlsxPreflightError(ValueError):
    """Stable rejection; the code is part of the preflight contract."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class XlsxSheetPreflight(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    name: str = Field(min_length=1, max_length=31)
    row_count: int = Field(ge=0, le=MAX_ROWS)
    cell_count: int = Field(ge=0, le=MAX_CELLS)
    formula_cells: int = Field(ge=0, le=MAX_CELLS)
    cached_formula_values: int = Field(ge=0, le=MAX_CELLS)
    formulas_without_cached_value: int = Field(ge=0, le=MAX_CELLS)

    @model_validator(mode="after")
    def formula_counts_match(self) -> "XlsxSheetPreflight":
        if self.cached_formula_values + self.formulas_without_cached_value != self.formula_cells:
            raise ValueError("xlsx preflight formula counts must add up")
        return self


class XlsxPreflightReport(ContractModel):
    """Observed structure only; no cell value, formula result or date is read out."""

    schema_version: Literal["1.0"] = "1.0"
    inspector: Literal["gda-xlsx-preflight-v1"] = "gda-xlsx-preflight-v1"
    content_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    byte_count: int = Field(ge=1, le=MAX_BYTES)
    zip_entries: int = Field(ge=1, le=MAX_ENTRIES)
    uncompressed_bytes: int = Field(ge=0, le=MAX_TOTAL_UNCOMPRESSED)
    shared_strings: int = Field(ge=0, le=MAX_SHARED_STRINGS)
    date_system: Literal["1900", "1904"]
    sheets: tuple[XlsxSheetPreflight, ...] = Field(min_length=1, max_length=MAX_SHEETS)
    macro_parts: Literal["none"] = "none"
    external_link_parts: Literal["none"] = "none"
    embedded_object_parts: Literal["none"] = "none"
    cell_values: Literal["not-extracted"] = "not-extracted"
    formula_values: Literal["inert-formula-text-not-evaluated"] = "inert-formula-text-not-evaluated"


def preflight_xlsx(data: bytes) -> XlsxPreflightReport:
    """Validate one XLSX package without opening a workbook, extracting values or executing XML."""
    if not isinstance(data, bytes) or not data:
        raise XlsxPreflightError("xlsx_preflight_empty")
    if len(data) > MAX_BYTES:
        raise XlsxPreflightError("xlsx_preflight_too_large")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError):
        raise XlsxPreflightError("xlsx_preflight_not_zip") from None
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ENTRIES:
            raise XlsxPreflightError("xlsx_preflight_entry_limit")
        names: dict[str, zipfile.ZipInfo] = {}
        uncompressed = 0
        for info in infos:
            name = _entry_name(info.filename)
            if name in names:
                raise XlsxPreflightError("xlsx_preflight_unsafe_entry")
            names[name] = info
            if info.flag_bits & 0x1:
                raise XlsxPreflightError("xlsx_preflight_encrypted")
            if info.file_size > MAX_ENTRY_BYTES:
                raise XlsxPreflightError("xlsx_preflight_entry_too_large")
            if info.file_size > max(info.compress_size, 1) * MAX_COMPRESSION_RATIO:
                raise XlsxPreflightError("xlsx_preflight_expansion_limit")
            uncompressed += info.file_size
            if uncompressed > MAX_TOTAL_UNCOMPRESSED:
                raise XlsxPreflightError("xlsx_preflight_expansion_limit")
            lowered = name.casefold()
            if lowered.endswith("/vbaproject.bin"):
                raise XlsxPreflightError("xlsx_preflight_macro_not_allowed")
            if lowered.startswith("xl/externallinks/") or lowered == "xl/connections.xml":
                raise XlsxPreflightError("xlsx_preflight_external_links_not_allowed")
            if lowered.startswith("xl/querytables/"):
                raise XlsxPreflightError("xlsx_preflight_external_links_not_allowed")
            if lowered.startswith("xl/embeddings/"):
                raise XlsxPreflightError("xlsx_preflight_embedded_objects_not_allowed")
        missing = [part for part in REQUIRED_PARTS if part not in names]
        if missing:
            raise XlsxPreflightError("xlsx_preflight_package_invalid")

        content_types = _xml(
            _read(archive, names["[Content_Types].xml"]), "xlsx_preflight_package_invalid"
        )
        if _local_name(content_types.tag) != "Types":
            raise XlsxPreflightError("xlsx_preflight_package_invalid")
        if any(
            "macroenabled" in (element.get("ContentType") or "").casefold()
            for element in content_types
        ):
            raise XlsxPreflightError("xlsx_preflight_macro_not_allowed")
        workbook = _xml(_read(archive, names["xl/workbook.xml"]), "xlsx_preflight_workbook_invalid")
        _require(workbook, f"{{{MAIN}}}workbook", "xlsx_preflight_workbook_invalid")
        relationships = _xml(
            _read(archive, names["xl/_rels/workbook.xml.rels"]),
            "xlsx_preflight_package_invalid",
        )
        _require(relationships, f"{{{PKG_REL}}}Relationships", "xlsx_preflight_package_invalid")
        targets = _sheet_targets(relationships, names)
        sheets = _sheets(workbook, targets)
        shared_strings = (
            _count_shared_strings(
                _xml(
                    _read(archive, names["xl/sharedStrings.xml"]),
                    "xlsx_preflight_shared_strings_invalid",
                )
            )
            if "xl/sharedStrings.xml" in names
            else 0
        )
        facts: list[XlsxSheetPreflight] = []
        total_cells = 0
        for name, part in sheets:
            fact, cells = _sheet(_read(archive, names[part]), name)
            total_cells += cells
            if total_cells > MAX_CELLS:
                raise XlsxPreflightError("xlsx_preflight_dimension_limit")
            facts.append(fact)
        date_system = _date_system(workbook)
    return XlsxPreflightReport(
        content_sha256="sha256:" + hashlib.sha256(data).hexdigest(),
        byte_count=len(data),
        zip_entries=len(infos),
        uncompressed_bytes=uncompressed,
        shared_strings=shared_strings,
        date_system=date_system,
        sheets=tuple(facts),
    )


def _entry_name(name: str) -> str:
    if not name or name.startswith("/") or "\\" in name:
        raise XlsxPreflightError("xlsx_preflight_unsafe_entry")
    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise XlsxPreflightError("xlsx_preflight_unsafe_entry")
    return name


def _read(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    limit = min(MAX_ENTRY_BYTES, MAX_TOTAL_UNCOMPRESSED)
    with archive.open(info) as stream:
        data = stream.read(limit + 1)
        if len(data) > limit or stream.read(1):
            raise XlsxPreflightError("xlsx_preflight_expansion_limit")
    if len(data) != info.file_size:
        raise XlsxPreflightError("xlsx_preflight_package_invalid")
    return data


def _xml(data: bytes, code: str) -> ElementTree.Element:
    # ElementTree does not resolve external entities, but declarations are rejected outright so a
    # future parser change cannot turn these parts into an expansion or fetch vector.
    lowered = data.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise XlsxPreflightError("xlsx_preflight_xml_declaration_not_allowed")
    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError:
        raise XlsxPreflightError(code) from None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _require(root: ElementTree.Element, tag: str, code: str) -> None:
    if root.tag != tag:
        raise XlsxPreflightError(code)


def _sheet_targets(relationships: ElementTree.Element, names: dict[str, zipfile.ZipInfo]):
    targets: dict[str, str] = {}
    for relation in relationships.findall(f"{{{PKG_REL}}}Relationship"):
        if relation.get("TargetMode", "Internal").casefold() == "external":
            raise XlsxPreflightError("xlsx_preflight_external_links_not_allowed")
        relation_id = relation.get("Id")
        target = relation.get("Target")
        relation_type = relation.get("Type", "")
        if not relation_id or not target:
            raise XlsxPreflightError("xlsx_preflight_package_invalid")
        if relation_type.endswith("/worksheet"):
            if target.startswith("/"):
                part = target.removeprefix("/")
            else:
                part = posixpath.normpath(posixpath.join("xl", target))
            if part.startswith("..") or part not in names:
                raise XlsxPreflightError("xlsx_preflight_sheet_invalid")
            targets[relation_id] = part
    return targets


def _sheets(workbook: ElementTree.Element, targets: dict[str, str]):
    sheets = workbook.find(f"{{{MAIN}}}sheets")
    entries = [] if sheets is None else list(sheets)
    if not 1 <= len(entries) <= MAX_SHEETS:
        raise XlsxPreflightError("xlsx_preflight_sheet_limit")
    result, names = [], set()
    for entry in entries:
        name = entry.get("name")
        relation_id = entry.get(f"{{{DOC_REL}}}id")
        if not name or not 1 <= len(name) <= 31 or name in names:
            raise XlsxPreflightError("xlsx_preflight_sheet_invalid")
        if not relation_id or relation_id not in targets:
            raise XlsxPreflightError("xlsx_preflight_sheet_invalid")
        names.add(name)
        result.append((name, targets[relation_id]))
    return result


def _date_system(workbook: ElementTree.Element) -> Literal["1900", "1904"]:
    properties = workbook.find(f"{{{MAIN}}}workbookPr")
    value = None if properties is None else properties.get("date1904")
    if value is None or value.casefold() in {"0", "false"}:
        return "1900"
    if value.casefold() in {"1", "true"}:
        return "1904"
    raise XlsxPreflightError("xlsx_preflight_workbook_invalid")


def _count_shared_strings(root: ElementTree.Element) -> int:
    _require(root, f"{{{MAIN}}}sst", "xlsx_preflight_shared_strings_invalid")
    count = sum(1 for _ in root.findall(f"{{{MAIN}}}si"))
    if count > MAX_SHARED_STRINGS:
        raise XlsxPreflightError("xlsx_preflight_shared_strings_limit")
    return count


def _column_index(reference: str | None) -> int:
    if not reference:
        return 0
    letters = ""
    for character in reference:
        if not character.isalpha():
            break
        letters += character.upper()
    digits = reference[len(letters) :]
    if not letters or not letters.isascii() or not digits.isascii() or not digits.isdigit():
        raise XlsxPreflightError("xlsx_preflight_sheet_invalid")
    index = 0
    for character in letters:
        index = index * 26 + (ord(character) - 64)
    return index


def _sheet(data: bytes, name: str) -> tuple[XlsxSheetPreflight, int]:
    root = _xml(data, "xlsx_preflight_sheet_invalid")
    _require(root, f"{{{MAIN}}}worksheet", "xlsx_preflight_sheet_invalid")
    rows = root.findall(f".//{{{MAIN}}}row")
    if len(rows) > MAX_ROWS:
        raise XlsxPreflightError("xlsx_preflight_dimension_limit")
    cells = formulas = cached = missing = 0
    for row in rows:
        for cell in row.findall(f"{{{MAIN}}}c"):
            cells += 1
            if cells > MAX_CELLS:
                raise XlsxPreflightError("xlsx_preflight_dimension_limit")
            if _column_index(cell.get("r")) > MAX_COLUMNS:
                raise XlsxPreflightError("xlsx_preflight_dimension_limit")
            formula = cell.find(f"{{{MAIN}}}f")
            if formula is None:
                continue
            formulas += 1
            if cell.find(f"{{{MAIN}}}v") is None:
                missing += 1
            else:
                cached += 1
    return (
        XlsxSheetPreflight(
            name=name,
            row_count=len(rows),
            cell_count=cells,
            formula_cells=formulas,
            cached_formula_values=cached,
            formulas_without_cached_value=missing,
        ),
        cells,
    )
