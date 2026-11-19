"""Bind one current declared XLSX snapshot into an immutable read-only analysis dataset.

The binder projects only explicitly selected declared columns from the values already retained
for the current declaration. Every selected cell must be a resolved value: missing, unparsed and
formula-cached cells are counted and refuse the whole binding instead of becoming nulls or guessed
values. Declared intent maps to an exact analysis type without numeric coercion: decimal and date
values remain canonical text, so this port never introduces floating-point or timezone semantics.

The result is one DLP-scanned, creator-bound artifact containing typed rows and their source row
numbers. It is not a `DatasetSnapshot`, never assigns target/feature roles, and never registers a
certified metric source.
"""

import hashlib
import json
import sqlite3
from bisect import bisect_right
from collections import Counter
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, StrictInt, model_validator

from generaldataagent.artifacts.governed import ArtifactReadPolicy, GovernedArtifactStore
from generaldataagent.contracts import ActorContext, ArtifactRef, Budget
from generaldataagent.contracts.models import ContractModel
from generaldataagent.datasets.xlsx_intake import XlsxIntakeService
from generaldataagent.datasets.xlsx_intake_contracts import (
    MAX_DECLARED_HEADER_CHARS,
    MAX_SNAPSHOT_CELLS,
    DeclaredColumnType,
    XlsxSnapshotColumnData,
    XlsxSnapshotData,
    _XlsxPayload,
)
from generaldataagent.datasets.xlsx_intake_store import XlsxIntakeConflict, XlsxIntakeUnavailable
from generaldataagent.datasets.xlsx_preflight import MAX_ROWS
from generaldataagent.datasets.xlsx_preview import MAX_CELL_CHARS, PREVIEW_COLUMNS
from generaldataagent.datasets.xlsx_snapshot import (
    MAX_PAGE_CHARS,
    MAX_PAGE_ROWS,
    SCAN_CHUNK_CHARS,
    SnapshotHash,
)
from generaldataagent.policy.budgets import TaskBudgetLedger

_ARTIFACT_TYPE = "xlsx_analysis_dataset"
_READER = "gda-xlsx-analysis-dataset-v1"
_BINDING_READER = "gda-xlsx-analysis-binding-v1"
_VALUES_READER = "gda-xlsx-analysis-values-v1"
_METRIC_READER = "gda-xlsx-analysis-metric-v1"
_CONVERSIONS: dict[str, tuple[str, str]] = {
    "text": ("string", "exact-text"),
    "integer": ("integer", "exact-integer"),
    "boolean": ("boolean", "exact-boolean"),
    "decimal": ("string", "exact-decimal-text"),
    "date": ("string", "iso-date-text"),
}


class XlsxAnalysisRefused(ValueError):
    """One selected column cannot enter analysis exactly as declared."""

    def __init__(self, code: str, *, columns: dict[int, dict[str, int]] | None = None):
        super().__init__(code)
        self.code = code
        self.columns = columns or {}


class BindXlsxAnalysis(ContractModel):
    """Explicit column subset over one exact retained snapshot version."""

    schema_version: Literal["1.0"] = "1.0"
    snapshot_hash: SnapshotHash
    columns: tuple[StrictInt, ...] = Field(min_length=1, max_length=PREVIEW_COLUMNS)

    @model_validator(mode="after")
    def columns_are_sorted_unique(self) -> "BindXlsxAnalysis":
        if any(
            type(index) is not int
            or not 0 <= index < PREVIEW_COLUMNS
            or (position and index <= self.columns[position - 1])
            for position, index in enumerate(self.columns)
        ):
            raise ValueError("xlsx_analysis_columns_invalid")
        return self


class XlsxAnalysisColumn(ContractModel):
    """One declared column and the exact analysis type it becomes."""

    schema_version: Literal["1.0"] = "1.0"
    index: int = Field(ge=0, lt=PREVIEW_COLUMNS, strict=True)
    header: str = Field(max_length=MAX_DECLARED_HEADER_CHARS)
    declared_type: DeclaredColumnType
    analysis_type: Literal["string", "integer", "boolean"]
    conversion: Literal[
        "exact-text",
        "exact-integer",
        "exact-boolean",
        "exact-decimal-text",
        "iso-date-text",
    ]

    @model_validator(mode="after")
    def conversion_matches_declaration(self) -> "XlsxAnalysisColumn":
        if (self.analysis_type, self.conversion) != _CONVERSIONS[self.declared_type]:
            raise ValueError("xlsx_analysis_column_mapping_invalid")
        return self


class XlsxAnalysisArtifact(ContractModel):
    """The complete creator-bound artifact body: typed rows plus source row numbers."""

    schema_version: Literal["1.0"] = "1.0"
    reader: Literal["gda-xlsx-analysis-dataset-v1"] = _READER
    file_id: UUID
    task_id: UUID
    source_hash: SnapshotHash
    preview_hash: SnapshotHash
    confirmation_hash: SnapshotHash
    interpretation_hash: SnapshotHash
    snapshot_hash: SnapshotHash
    date_system: Literal["1900", "1904"]
    duplicate_rows: Literal["reject", "keep-all"]
    duplicate_row_count: int = Field(ge=0, le=MAX_ROWS, strict=True)
    columns: tuple[XlsxAnalysisColumn, ...] = Field(min_length=1, max_length=PREVIEW_COLUMNS)
    row_numbers: tuple[int, ...] = Field(min_length=2, max_length=MAX_ROWS)
    rows: tuple[tuple[Any, ...], ...] = Field(min_length=2, max_length=MAX_ROWS)

    @model_validator(mode="after")
    def rows_match_columns_and_types(self) -> "XlsxAnalysisArtifact":
        indices = [column.index for column in self.columns]
        names = [column.header for column in self.columns]
        if (
            indices != sorted(set(indices))
            or len(names) != len(set(names))
            or any(not name or len(name) > MAX_DECLARED_HEADER_CHARS for name in names)
            or len(self.row_numbers) != len(self.rows)
            or len(self.rows) * len(self.columns) > MAX_SNAPSHOT_CELLS
            or any(
                not 1 <= row <= MAX_ROWS or (position and row <= self.row_numbers[position - 1])
                for position, row in enumerate(self.row_numbers)
            )
        ):
            raise ValueError("xlsx_analysis_artifact_invalid")
        for row in self.rows:
            if len(row) != len(self.columns):
                raise ValueError("xlsx_analysis_artifact_invalid")
            for column, value in zip(self.columns, row, strict=True):
                if column.analysis_type == "string":
                    if type(value) is not str or not 1 <= len(value) <= MAX_CELL_CHARS:
                        raise ValueError("xlsx_analysis_artifact_invalid")
                elif column.analysis_type == "integer":
                    if type(value) is not int:
                        raise ValueError("xlsx_analysis_artifact_invalid")
                elif type(value) is not bool:
                    raise ValueError("xlsx_analysis_artifact_invalid")
        return self


class XlsxAnalysisDataset(ContractModel):
    """Metadata for one immutable binding; values stay inside the creator-bound artifact."""

    schema_version: Literal["1.0"] = "1.0"
    reader: Literal["gda-xlsx-analysis-binding-v1"] = _BINDING_READER
    file_id: UUID
    task_id: UUID
    source_hash: SnapshotHash
    preview_hash: SnapshotHash
    confirmation_hash: SnapshotHash
    interpretation_hash: SnapshotHash
    snapshot_hash: SnapshotHash
    date_system: Literal["1900", "1904"]
    duplicate_rows: Literal["reject", "keep-all"]
    duplicate_row_count: int = Field(ge=0, le=MAX_ROWS, strict=True)
    columns: tuple[XlsxAnalysisColumn, ...] = Field(min_length=1, max_length=PREVIEW_COLUMNS)
    row_count: int = Field(ge=2, le=MAX_ROWS, strict=True)
    cells: int = Field(ge=2, strict=True)
    created_at: AwareDatetime
    artifact: ArtifactRef
    scope: Literal["selected-declared-columns"] = "selected-declared-columns"
    values: Literal["typed-exact-not-returned"] = "typed-exact-not-returned"
    storage: Literal["dlp-scanned-creator-artifact"] = "dlp-scanned-creator-artifact"
    rows: Literal["present-value-only"] = "present-value-only"
    roles: Literal["not-assigned"] = "not-assigned"
    target: Literal["not-designated"] = "not-designated"
    duplicates: Literal["all-present-rows-kept"] = "all-present-rows-kept"
    formula_cached_values: Literal["excluded"] = "excluded"
    missing_values: Literal["excluded"] = "excluded"
    dataset_snapshot: Literal[False] = False
    certified_metric_source: Literal[False] = False

    @model_validator(mode="after")
    def report_is_coherent(self) -> "XlsxAnalysisDataset":
        if self.cells != self.row_count * len(self.columns):
            raise ValueError("xlsx_analysis_report_invalid")
        if self.artifact.artifact_type != _ARTIFACT_TYPE or self.artifact.task_id != self.task_id:
            raise ValueError("xlsx_analysis_report_invalid")
        return self


class XlsxAnalysisValueRow(ContractModel):
    """One typed source row from an analysis dataset; every value is present."""

    schema_version: Literal["1.0"] = "1.0"
    row: int = Field(ge=1, le=MAX_ROWS, strict=True)
    values: tuple[Any, ...] = Field(max_length=PREVIEW_COLUMNS)


class XlsxAnalysisValuePage(ContractModel):
    """A keyset page of exact typed values from one creator-bound analysis dataset."""

    schema_version: Literal["1.0"] = "1.0"
    reader: Literal["gda-xlsx-analysis-values-v1"] = _VALUES_READER
    file_id: UUID
    task_id: UUID
    artifact_id: UUID
    artifact_hash: SnapshotHash
    snapshot_hash: SnapshotHash
    status: Literal["current"] = "current"
    columns: tuple[XlsxAnalysisColumn, ...] = Field(min_length=1, max_length=PREVIEW_COLUMNS)
    rows: tuple[XlsxAnalysisValueRow, ...] = Field(max_length=MAX_PAGE_ROWS)
    after: int | None = Field(default=None, ge=0, strict=True)
    next: int | None = Field(default=None, ge=0, strict=True)
    more: bool = Field(strict=True)
    retained_rows: int = Field(ge=2, le=MAX_ROWS, strict=True)
    values: Literal["typed-exact-declared-columns-only"] = "typed-exact-declared-columns-only"
    dlp: Literal["artifact-rescanned-before-return"] = "artifact-rescanned-before-return"
    storage: Literal["dlp-scanned-creator-artifact"] = "dlp-scanned-creator-artifact"
    row_scope: Literal["present-value-only"] = "present-value-only"
    roles: Literal["not-assigned"] = "not-assigned"
    target: Literal["not-designated"] = "not-designated"
    dataset_snapshot: Literal[False] = False
    certified_metric_source: Literal[False] = False

    @model_validator(mode="after")
    def page_is_coherent(self) -> "XlsxAnalysisValuePage":
        indices = [column.index for column in self.columns]
        names = [column.header for column in self.columns]
        if (
            indices != sorted(set(indices))
            or len(names) != len(set(names))
            or any(not name or len(name) > MAX_DECLARED_HEADER_CHARS for name in names)
            or self.more != (self.next is not None)
            or len(self.rows) > self.retained_rows
            or (self.more and not self.rows)
            or (self.more and self.next != self.rows[-1].row)
        ):
            raise ValueError("xlsx_analysis_page_invalid")
        characters = 0
        for position, row in enumerate(self.rows):
            if len(row.values) != len(self.columns):
                raise ValueError("xlsx_analysis_page_invalid")
            if position and self.rows[position - 1].row >= row.row:
                raise ValueError("xlsx_analysis_page_invalid")
            if self.after is not None and row.row <= self.after:
                raise ValueError("xlsx_analysis_page_invalid")
            for column, value in zip(self.columns, row.values, strict=True):
                if column.analysis_type == "string":
                    if type(value) is not str or not 1 <= len(value) <= MAX_CELL_CHARS:
                        raise ValueError("xlsx_analysis_page_invalid")
                    characters += len(value)
                elif column.analysis_type == "integer":
                    if type(value) is not int:
                        raise ValueError("xlsx_analysis_page_invalid")
                    characters += len(str(value))
                else:
                    if type(value) is not bool:
                        raise ValueError("xlsx_analysis_page_invalid")
                    characters += 5
        if characters > MAX_PAGE_CHARS:
            raise ValueError("xlsx_analysis_page_invalid")
        return self


class XlsxAnalysisColumnProfile(ContractModel):
    """Exact cardinality for one analysis column; extremes only for native integers."""

    schema_version: Literal["1.0"] = "1.0"
    index: int = Field(ge=0, lt=PREVIEW_COLUMNS, strict=True)
    header: str = Field(max_length=MAX_DECLARED_HEADER_CHARS)
    declared_type: DeclaredColumnType
    analysis_type: Literal["string", "integer", "boolean"]
    conversion: Literal[
        "exact-text",
        "exact-integer",
        "exact-boolean",
        "exact-decimal-text",
        "iso-date-text",
    ]
    present_count: int = Field(ge=1, le=MAX_ROWS, strict=True)
    distinct_count: int = Field(ge=1, le=MAX_ROWS, strict=True)
    integer_min: StrictInt | None = None
    integer_max: StrictInt | None = None

    @model_validator(mode="after")
    def counts_and_extremes_are_coherent(self) -> "XlsxAnalysisColumnProfile":
        if (
            not self.header
            or (self.analysis_type, self.conversion) != _CONVERSIONS[self.declared_type]
            or self.distinct_count > self.present_count
            or (self.integer_min is None) != (self.analysis_type != "integer")
            or (self.integer_max is None) != (self.analysis_type != "integer")
            or (
                self.integer_min is not None
                and self.integer_max is not None
                and self.integer_min > self.integer_max
            )
        ):
            raise ValueError("xlsx_analysis_profile_invalid")
        return self


class XlsxAnalysisProfile(ContractModel):
    """Bounded counts for one immutable analysis dataset; never a metric query or result."""

    schema_version: Literal["1.0"] = "1.0"
    reader: Literal["gda-xlsx-analysis-profile-v1"] = "gda-xlsx-analysis-profile-v1"
    file_id: UUID
    task_id: UUID
    artifact_id: UUID
    artifact_hash: SnapshotHash
    snapshot_hash: SnapshotHash
    status: Literal["current"] = "current"
    row_count: int = Field(ge=2, le=MAX_ROWS, strict=True)
    cells: int = Field(ge=2, le=MAX_SNAPSHOT_CELLS, strict=True)
    columns: tuple[XlsxAnalysisColumnProfile, ...] = Field(min_length=1, max_length=PREVIEW_COLUMNS)
    scope: Literal["selected-declared-columns"] = "selected-declared-columns"
    aggregation: Literal["exact-counts-and-integer-extremes"] = "exact-counts-and-integer-extremes"
    values: Literal["counts-only-not-returned"] = "counts-only-not-returned"
    dlp: Literal["header-text-rescanned-before-return"] = "header-text-rescanned-before-return"
    storage: Literal["dlp-scanned-creator-artifact"] = "dlp-scanned-creator-artifact"
    row_scope: Literal["present-value-only"] = "present-value-only"
    roles: Literal["not-assigned"] = "not-assigned"
    target: Literal["not-designated"] = "not-designated"
    dataset_snapshot: Literal[False] = False
    certified_metric_source: Literal[False] = False

    @model_validator(mode="after")
    def profile_is_coherent(self) -> "XlsxAnalysisProfile":
        indices = [column.index for column in self.columns]
        names = [column.header for column in self.columns]
        if (
            indices != sorted(set(indices))
            or len(names) != len(set(names))
            or self.cells != self.row_count * len(self.columns)
            or any(column.present_count != self.row_count for column in self.columns)
        ):
            raise ValueError("xlsx_analysis_profile_invalid")
        return self


AnalysisMetric = Literal[
    "row-count",
    "distinct-count",
    "integer-sum",
    "integer-min",
    "integer-max",
]


class ReadXlsxAnalysisMetric(ContractModel):
    """One explicit exact aggregate over one immutable analysis dataset."""

    schema_version: Literal["1.0"] = "1.0"
    snapshot_hash: SnapshotHash
    metric: AnalysisMetric
    column: StrictInt | None = Field(default=None, ge=0, lt=PREVIEW_COLUMNS)

    @model_validator(mode="after")
    def column_matches_metric(self) -> "ReadXlsxAnalysisMetric":
        if (self.metric == "row-count") != (self.column is None):
            raise ValueError("xlsx_analysis_metric_column_invalid")
        return self


class XlsxAnalysisMetric(ContractModel):
    """One exact integer aggregate; explicitly not a certified metric query or result."""

    schema_version: Literal["1.0"] = "1.0"
    reader: Literal["gda-xlsx-analysis-metric-v1"] = _METRIC_READER
    file_id: UUID
    task_id: UUID
    artifact_id: UUID
    artifact_hash: SnapshotHash
    snapshot_hash: SnapshotHash
    status: Literal["current"] = "current"
    metric: AnalysisMetric
    column: XlsxAnalysisColumn | None = None
    value: StrictInt
    row_count: int = Field(ge=2, le=MAX_ROWS, strict=True)
    scope: Literal["one-creator-bound-analysis-artifact"] = "one-creator-bound-analysis-artifact"
    aggregation: Literal["exact-integer-single-pass"] = "exact-integer-single-pass"
    qualification: Literal["non-certified-analysis-view"] = "non-certified-analysis-view"
    metric_catalog: Literal["not-registered"] = "not-registered"
    metric_definition: Literal["not-resolved"] = "not-resolved"
    result_view: Literal["analysis-metric-view-not-metric-result"] = (
        "analysis-metric-view-not-metric-result"
    )
    dlp: Literal["header-text-rescanned-before-return", "no-text-disclosed"]
    storage: Literal["dlp-scanned-creator-artifact"] = "dlp-scanned-creator-artifact"
    row_scope: Literal["present-value-only"] = "present-value-only"
    roles: Literal["not-assigned"] = "not-assigned"
    target: Literal["not-designated"] = "not-designated"
    dataset_snapshot: Literal[False] = False
    certified_metric_source: Literal[False] = False

    @model_validator(mode="after")
    def metric_is_coherent(self) -> "XlsxAnalysisMetric":
        if (self.metric == "row-count") != (self.column is None):
            raise ValueError("xlsx_analysis_metric_invalid")
        if (
            self.column is not None
            and self.metric != "distinct-count"
            and self.column.analysis_type != "integer"
        ):
            raise ValueError("xlsx_analysis_metric_invalid")
        if self.dlp != (
            "no-text-disclosed" if self.column is None else "header-text-rescanned-before-return"
        ):
            raise ValueError("xlsx_analysis_metric_invalid")
        if self.metric == "row-count" and self.value != self.row_count:
            raise ValueError("xlsx_analysis_metric_invalid")
        if self.metric == "distinct-count" and not 1 <= self.value <= self.row_count:
            raise ValueError("xlsx_analysis_metric_invalid")
        return self


def _analysis_column(column: XlsxSnapshotColumnData) -> XlsxAnalysisColumn:
    analysis_type, conversion = _CONVERSIONS[column.type]
    return XlsxAnalysisColumn(
        index=column.index,
        header=column.header,
        declared_type=column.type,
        analysis_type=analysis_type,
        conversion=conversion,
    )


def _exact_value(column: XlsxSnapshotColumnData, value: str) -> bool | int | str:
    if column.type in ("text", "decimal", "date"):
        return value
    if column.type == "boolean":
        if value == "true":
            return True
        if value == "false":
            return False
        raise XlsxAnalysisRefused("xlsx_analysis_value_invalid")
    try:
        return int(value)
    except ValueError as exc:
        raise XlsxAnalysisRefused("xlsx_analysis_value_invalid") from exc


def analysis_page(
    artifact: XlsxAnalysisArtifact,
    *,
    artifact_id: UUID,
    artifact_hash: str,
    after: int | None = None,
    limit: int = MAX_PAGE_ROWS,
) -> XlsxAnalysisValuePage:
    """Project a keyset page of already-typed values; no value leaves through metadata."""
    if not 1 <= limit <= MAX_PAGE_ROWS:
        raise XlsxAnalysisRefused("xlsx_analysis_page_limit_invalid")
    start = 0 if after is None else bisect_right(artifact.row_numbers, after)
    stop = min(start + limit, len(artifact.row_numbers))
    rows = []
    characters = 0
    for position in range(start, stop):
        values = artifact.rows[position]
        characters += sum(
            len(value) if type(value) is str else 5 if type(value) is bool else len(str(value))
            for value in values
        )
        rows.append(XlsxAnalysisValueRow(row=artifact.row_numbers[position], values=values))
    if characters > MAX_PAGE_CHARS:
        raise XlsxAnalysisRefused("xlsx_analysis_page_char_limit")
    more = stop < len(artifact.row_numbers)
    return XlsxAnalysisValuePage(
        file_id=artifact.file_id,
        task_id=artifact.task_id,
        artifact_id=artifact_id,
        artifact_hash=artifact_hash,
        snapshot_hash=artifact.snapshot_hash,
        columns=artifact.columns,
        rows=tuple(rows),
        after=after,
        next=rows[-1].row if more and rows else None,
        more=more,
        retained_rows=len(artifact.row_numbers),
    )


def analysis_profile(
    artifact: XlsxAnalysisArtifact, *, artifact_id: UUID, artifact_hash: str
) -> XlsxAnalysisProfile:
    """Count exact present values per column; never return a value or coerce a text type."""
    columns = []
    for position, column in enumerate(artifact.columns):
        values = [row[position] for row in artifact.rows]
        present = len(values)
        distinct = len(set(values))
        integer_min = min(values) if column.analysis_type == "integer" else None
        integer_max = max(values) if column.analysis_type == "integer" else None
        columns.append(
            XlsxAnalysisColumnProfile(
                index=column.index,
                header=column.header,
                declared_type=column.declared_type,
                analysis_type=column.analysis_type,
                conversion=column.conversion,
                present_count=present,
                distinct_count=distinct,
                integer_min=integer_min,
                integer_max=integer_max,
            )
        )
    return XlsxAnalysisProfile(
        file_id=artifact.file_id,
        task_id=artifact.task_id,
        artifact_id=artifact_id,
        artifact_hash=artifact_hash,
        snapshot_hash=artifact.snapshot_hash,
        row_count=len(artifact.row_numbers),
        cells=len(artifact.row_numbers) * len(artifact.columns),
        columns=tuple(columns),
    )


def analysis_metric(
    artifact: XlsxAnalysisArtifact,
    *,
    artifact_id: UUID,
    artifact_hash: str,
    request: ReadXlsxAnalysisMetric,
) -> XlsxAnalysisMetric:
    """Compute one exact integer aggregate without disclosing any cell value."""
    request = ReadXlsxAnalysisMetric.model_validate(request.model_dump())
    column = None
    value = len(artifact.row_numbers)
    if request.column is not None:
        position = next(
            (index for index, item in enumerate(artifact.columns) if item.index == request.column),
            None,
        )
        if position is None:
            raise XlsxAnalysisRefused("xlsx_analysis_metric_column_not_bound")
        column = artifact.columns[position]
        values = [row[position] for row in artifact.rows]
        if request.metric == "distinct-count":
            value = len(set(values))
        elif column.analysis_type != "integer":
            raise XlsxAnalysisRefused("xlsx_analysis_metric_type_invalid")
        elif request.metric == "integer-sum":
            value = sum(values)
        elif request.metric == "integer-min":
            value = min(values)
        else:
            value = max(values)
    return XlsxAnalysisMetric(
        file_id=artifact.file_id,
        task_id=artifact.task_id,
        artifact_id=artifact_id,
        artifact_hash=artifact_hash,
        snapshot_hash=artifact.snapshot_hash,
        metric=request.metric,
        column=column,
        value=value,
        row_count=len(artifact.row_numbers),
        dlp="no-text-disclosed" if column is None else "header-text-rescanned-before-return",
    )


def _scan_page_values(
    rows: tuple[XlsxAnalysisValueRow, ...], scanner, check: Callable[[], None]
) -> None:
    """Rescan the page's plaintext before disclosure; a denial returns no page."""
    chunk: list[str] = []
    size = 0
    for row in rows:
        for value in row.values:
            text = "true" if value is True else "false" if value is False else str(value)
            chunk.append(text)
            size += len(text) + 1
            if size >= SCAN_CHUNK_CHARS:
                check()
                scanner.check("\n".join(chunk).encode())
                chunk, size = [], 0
    if chunk:
        check()
        scanner.check("\n".join(chunk).encode())


def _scan_headers(columns: tuple[XlsxAnalysisColumnProfile, ...], scanner, check) -> None:
    """Rescan the only disclosed text before returning counts; no cell value is scanned here."""
    check()
    scanner.check("\n".join(column.header for column in columns).encode())
    check()


def _scan_metric_header(column: XlsxAnalysisColumn, scanner, check) -> None:
    """Rescan the one disclosed header before returning its aggregate."""
    check()
    scanner.check(column.header.encode())
    check()


class XlsxAnalysisBinder:
    """Write one immutable analysis dataset per explicit creator request.

    Every call appends a new artifact; identical inputs produce identical content and content
    hash. The binder never rewrites source bytes, declaration, snapshot or artifact metadata.
    """

    def __init__(
        self,
        *,
        intake: XlsxIntakeService,
        budgets: TaskBudgetLedger,
        artifact_root: Path,
        artifact_index: Path,
    ):
        root = artifact_root.resolve()
        if any(
            path.resolve().is_relative_to(root)
            for path in (intake.store.path, budgets.path, artifact_index)
        ):
            raise ValueError("xlsx_analysis_state_must_be_outside_artifact_content")
        self.intake, self.budgets = intake, budgets
        self.artifact_root, self.artifact_index = artifact_root, artifact_index

    def _current(
        self,
        file_id: UUID,
        actor: ActorContext,
        token: str,
        snapshot_hash: str,
        deadline: float,
    ) -> tuple[_XlsxPayload, XlsxSnapshotData, Callable[[], None]]:
        check = self.intake.guard(actor, token, deadline=deadline)
        with self.intake.store.db() as db:
            _, payload, raw = self.intake.store.owned(db, file_id, actor)
            bound = self.intake._bound_guard(payload, check)
            snapshot = payload.snapshot
            if snapshot is None:
                raise XlsxIntakeConflict("xlsx_snapshot_not_derived")
            if (
                payload.interpretation is None
                or payload.interpretation.interpretation_hash != snapshot.interpretation_hash
            ):
                raise XlsxIntakeConflict("xlsx_snapshot_stale")
            if snapshot.snapshot_hash != snapshot_hash:
                raise XlsxIntakeConflict("xlsx_snapshot_changed")
            self.intake._parsed(payload, raw, bound)
            bound()
            return payload, snapshot, check

    @staticmethod
    def _project(
        payload: _XlsxPayload,
        snapshot: XlsxSnapshotData,
        indices: tuple[int, ...],
        check: Callable[[], None],
    ) -> XlsxAnalysisArtifact:
        by_index = {column.index: column for column in snapshot.columns}
        if any(index not in by_index for index in indices):
            raise XlsxAnalysisRefused("xlsx_analysis_column_not_declared")
        columns = tuple(by_index[index] for index in indices)
        if any(set(column.statuses) != {"v"} for column in columns):
            counts = {column.index: dict(Counter(column.statuses)) for column in columns}
            raise XlsxAnalysisRefused("xlsx_analysis_non_value_cells", columns=counts)
        names = [column.header for column in columns]
        if any(not name for name in names) or len(names) != len(set(names)):
            raise XlsxAnalysisRefused("xlsx_analysis_column_names_invalid")
        if len(snapshot.rows) < 2:
            raise XlsxAnalysisRefused("xlsx_analysis_rows_insufficient")
        rows = []
        for position, _ in enumerate(snapshot.rows):
            if position % 256 == 0:
                check()
            rows.append(tuple(_exact_value(column, column.values[position]) for column in columns))
        check()
        return XlsxAnalysisArtifact(
            file_id=payload.file.file_id,
            task_id=payload.file.task_id,
            source_hash=snapshot.source_hash,
            preview_hash=snapshot.preview_hash,
            confirmation_hash=snapshot.confirmation_hash,
            interpretation_hash=snapshot.interpretation_hash,
            snapshot_hash=snapshot.snapshot_hash,
            date_system=snapshot.date_system,
            duplicate_rows=snapshot.duplicate_rows,
            duplicate_row_count=snapshot.duplicate_row_count,
            columns=tuple(_analysis_column(column) for column in columns),
            row_numbers=snapshot.rows,
            rows=tuple(rows),
        )

    def _store(self, actor: ActorContext) -> GovernedArtifactStore:
        return GovernedArtifactStore(
            self.artifact_root,
            index_path=self.artifact_index,
            actor=actor,
            scanner=self.intake.scanner,
            retention_seconds=self.intake.retention_seconds,
            clock=self.intake.store.clock,
            read_policy=ArtifactReadPolicy.CREATOR,
        )

    def _write(
        self, artifact: XlsxAnalysisArtifact, actor: ActorContext
    ) -> tuple[GovernedArtifactStore, ArtifactRef]:
        content = json.dumps(
            artifact.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        artifacts = self._store(actor)
        try:
            ref = artifacts.put_bytes(
                task_id=artifact.task_id,
                artifact_type=_ARTIFACT_TYPE,
                content=content,
                created_by=actor.user_id,
                extension="json",
            )
        except (OSError, sqlite3.Error) as exc:
            raise XlsxIntakeUnavailable("xlsx_analysis_store_unavailable") from exc
        return artifacts, ref

    def bind(
        self,
        file_id: UUID,
        actor: ActorContext,
        token: str,
        request: BindXlsxAnalysis,
    ) -> XlsxAnalysisDataset:
        request = BindXlsxAnalysis.model_validate(request.model_dump())
        deadline = self.intake.monotonic() + self.intake.limits.max_seconds
        payload, snapshot, check = self._current(
            file_id, actor, token, request.snapshot_hash, deadline
        )
        projected = self._project(payload, snapshot, request.columns, check)
        artifacts, artifact = self._write(projected, actor)
        try:
            self.intake._audit(
                "xlsx.analysis_dataset_bound",
                payload,
                {
                    "snapshot_hash": snapshot.snapshot_hash,
                    "artifact_id": str(artifact.artifact_id),
                    "columns": list(request.columns),
                    "rows": len(snapshot.rows),
                    "cells": len(snapshot.rows) * len(request.columns),
                },
            )
            current, _, after = self._current(
                file_id, actor, token, request.snapshot_hash, deadline
            )
            after()
            if current.snapshot is None or current.snapshot.snapshot_hash != snapshot.snapshot_hash:
                raise XlsxIntakeConflict("xlsx_snapshot_changed")
            return XlsxAnalysisDataset(
                file_id=payload.file.file_id,
                task_id=payload.file.task_id,
                source_hash=snapshot.source_hash,
                preview_hash=snapshot.preview_hash,
                confirmation_hash=snapshot.confirmation_hash,
                interpretation_hash=snapshot.interpretation_hash,
                snapshot_hash=snapshot.snapshot_hash,
                date_system=snapshot.date_system,
                duplicate_rows=snapshot.duplicate_rows,
                duplicate_row_count=snapshot.duplicate_row_count,
                columns=projected.columns,
                row_count=len(snapshot.rows),
                cells=len(snapshot.rows) * len(projected.columns),
                created_at=artifact.created_at,
                artifact=artifact,
            )
        except Exception:
            with suppress(Exception):
                artifacts.discard(artifact)
            raise

    def values(
        self,
        file_id: UUID,
        artifact_id: UUID,
        actor: ActorContext,
        token: str,
        *,
        snapshot_hash: str,
        after: int | None = None,
        limit: int = 100,
        deadline: float | None = None,
    ) -> XlsxAnalysisValuePage:
        """Return a bounded keyset page from one exact creator-bound binding."""
        if not 1 <= limit <= MAX_PAGE_ROWS:
            raise XlsxAnalysisRefused("xlsx_analysis_page_limit_invalid")
        deadline = (
            self.intake.monotonic() + self.intake.limits.max_seconds
            if deadline is None
            else deadline
        )
        payload, snapshot, check = self._current(file_id, actor, token, snapshot_hash, deadline)
        artifacts = self._store(actor)
        reference = artifacts.find_task_artifact(payload.file.task_id, artifact_id, _ARTIFACT_TYPE)
        if reference is None:
            raise PermissionError("xlsx_analysis_artifact_unavailable")
        check()
        parsed = load_xlsx_analysis_artifact(artifacts.read_bytes(reference), artifact=reference)
        check()
        if parsed.file_id != file_id or parsed.snapshot_hash != snapshot.snapshot_hash:
            raise XlsxIntakeConflict("xlsx_snapshot_changed")
        page = analysis_page(
            parsed,
            artifact_id=reference.artifact_id,
            artifact_hash=reference.content_hash,
            after=after,
            limit=limit,
        )
        if page.rows:
            _scan_page_values(page.rows, self.intake.scanner, check)
            self.intake._audit(
                "xlsx.analysis_values_read",
                payload,
                {
                    "snapshot_hash": snapshot.snapshot_hash,
                    "artifact_id": str(reference.artifact_id),
                    "rows": len(page.rows),
                    "cells": len(page.rows) * len(page.columns),
                    "first_row": page.rows[0].row,
                    "last_row": page.rows[-1].row,
                },
            )
        check()
        current, _, after_check = self._current(file_id, actor, token, snapshot_hash, deadline)
        after_check()
        if current.snapshot is None or current.snapshot.snapshot_hash != snapshot.snapshot_hash:
            raise XlsxIntakeConflict("xlsx_snapshot_changed")
        return page

    def profile(
        self,
        file_id: UUID,
        artifact_id: UUID,
        actor: ActorContext,
        token: str,
        *,
        snapshot_hash: str,
        deadline: float | None = None,
    ) -> XlsxAnalysisProfile:
        """Return exact bounded counts for one creator-bound analysis dataset."""
        deadline = (
            self.intake.monotonic() + self.intake.limits.max_seconds
            if deadline is None
            else deadline
        )
        payload, snapshot, check = self._current(file_id, actor, token, snapshot_hash, deadline)
        artifacts = self._store(actor)
        reference = artifacts.find_task_artifact(payload.file.task_id, artifact_id, _ARTIFACT_TYPE)
        if reference is None:
            raise PermissionError("xlsx_analysis_artifact_unavailable")
        check()
        parsed = load_xlsx_analysis_artifact(artifacts.read_bytes(reference), artifact=reference)
        check()
        if parsed.file_id != file_id or parsed.snapshot_hash != snapshot.snapshot_hash:
            raise XlsxIntakeConflict("xlsx_snapshot_changed")
        profile = analysis_profile(
            parsed,
            artifact_id=reference.artifact_id,
            artifact_hash=reference.content_hash,
        )
        _scan_headers(profile.columns, self.intake.scanner, check)
        self.intake._audit(
            "xlsx.analysis_profile_read",
            payload,
            {
                "snapshot_hash": snapshot.snapshot_hash,
                "artifact_id": str(reference.artifact_id),
                "rows": profile.row_count,
                "columns": len(profile.columns),
            },
        )
        check()
        current, _, after_check = self._current(file_id, actor, token, snapshot_hash, deadline)
        after_check()
        if current.snapshot is None or current.snapshot.snapshot_hash != snapshot.snapshot_hash:
            raise XlsxIntakeConflict("xlsx_snapshot_changed")
        return profile

    def metric(
        self,
        file_id: UUID,
        artifact_id: UUID,
        actor: ActorContext,
        token: str,
        *,
        snapshot_hash: str,
        metric: str,
        column: int | None = None,
        deadline: float | None = None,
    ) -> XlsxAnalysisMetric:
        """Return one exact aggregate from one creator-bound analysis artifact."""
        request = ReadXlsxAnalysisMetric(snapshot_hash=snapshot_hash, metric=metric, column=column)
        deadline = (
            self.intake.monotonic() + self.intake.limits.max_seconds
            if deadline is None
            else deadline
        )
        payload, snapshot, check = self._current(
            file_id, actor, token, request.snapshot_hash, deadline
        )
        artifacts = self._store(actor)
        reference = artifacts.find_task_artifact(payload.file.task_id, artifact_id, _ARTIFACT_TYPE)
        if reference is None:
            raise PermissionError("xlsx_analysis_artifact_unavailable")
        self.budgets.register(
            payload.file.task_id,
            payload.actor,
            Budget(max_runtime_seconds=self.intake.limits.max_seconds),
        )
        self.budgets.reserve(payload.file.task_id, payload.actor, scan_bytes=reference.size_bytes)
        check()
        parsed = load_xlsx_analysis_artifact(artifacts.read_bytes(reference), artifact=reference)
        check()
        if parsed.file_id != file_id or parsed.snapshot_hash != snapshot.snapshot_hash:
            raise XlsxIntakeConflict("xlsx_snapshot_changed")
        view = analysis_metric(
            parsed,
            artifact_id=reference.artifact_id,
            artifact_hash=reference.content_hash,
            request=request,
        )
        if view.column is not None:
            _scan_metric_header(view.column, self.intake.scanner, check)
        self.intake._audit(
            "xlsx.analysis_metric_read",
            payload,
            {
                "snapshot_hash": snapshot.snapshot_hash,
                "artifact_id": str(reference.artifact_id),
                "metric": view.metric,
                "column": view.column.index if view.column is not None else None,
                "value": view.value,
                "rows": view.row_count,
            },
        )
        check()
        current, _, after_check = self._current(
            file_id, actor, token, request.snapshot_hash, deadline
        )
        after_check()
        if current.snapshot is None or current.snapshot.snapshot_hash != snapshot.snapshot_hash:
            raise XlsxIntakeConflict("xlsx_snapshot_changed")
        return view


def load_xlsx_analysis_artifact(content: bytes, *, artifact: ArtifactRef) -> XlsxAnalysisArtifact:
    """Validate one already-authorized artifact body against its reference."""
    if artifact.artifact_type != _ARTIFACT_TYPE:
        raise ValueError("xlsx_analysis_artifact_type_invalid")
    if hashlib.sha256(content).hexdigest() != artifact.content_hash.removeprefix("sha256:"):
        raise ValueError("xlsx_analysis_artifact_changed")
    parsed = XlsxAnalysisArtifact.model_validate_json(content)
    if parsed.task_id != artifact.task_id:
        raise ValueError("xlsx_analysis_artifact_binding_invalid")
    return parsed


__all__ = [
    "AnalysisMetric",
    "BindXlsxAnalysis",
    "ReadXlsxAnalysisMetric",
    "XlsxAnalysisArtifact",
    "XlsxAnalysisBinder",
    "XlsxAnalysisColumn",
    "XlsxAnalysisColumnProfile",
    "XlsxAnalysisDataset",
    "XlsxAnalysisMetric",
    "XlsxAnalysisProfile",
    "XlsxAnalysisRefused",
    "XlsxAnalysisValuePage",
    "XlsxAnalysisValueRow",
    "analysis_metric",
    "analysis_page",
    "analysis_profile",
    "load_xlsx_analysis_artifact",
]
