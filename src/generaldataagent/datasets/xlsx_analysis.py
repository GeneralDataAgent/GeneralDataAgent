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
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, StrictInt, model_validator

from generaldataagent.artifacts.governed import ArtifactReadPolicy, GovernedArtifactStore
from generaldataagent.contracts import ActorContext, ArtifactRef
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
from generaldataagent.datasets.xlsx_snapshot import SnapshotHash

_ARTIFACT_TYPE = "xlsx_analysis_dataset"
_READER = "gda-xlsx-analysis-dataset-v1"
_BINDING_READER = "gda-xlsx-analysis-binding-v1"
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


class XlsxAnalysisBinder:
    """Write one immutable analysis dataset per explicit creator request.

    Every call appends a new artifact; identical inputs produce identical content and content
    hash. The binder never rewrites source bytes, declaration, snapshot or artifact metadata.
    """

    def __init__(
        self,
        *,
        intake: XlsxIntakeService,
        artifact_root: Path,
        artifact_index: Path,
    ):
        root = artifact_root.resolve()
        if any(path.resolve().is_relative_to(root) for path in (intake.store.path, artifact_index)):
            raise ValueError("xlsx_analysis_state_must_be_outside_artifact_content")
        self.intake, self.artifact_root, self.artifact_index = intake, artifact_root, artifact_index

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

    def _write(self, artifact: XlsxAnalysisArtifact, actor: ActorContext) -> ArtifactRef:
        content = json.dumps(
            artifact.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        artifacts = GovernedArtifactStore(
            self.artifact_root,
            index_path=self.artifact_index,
            actor=actor,
            scanner=self.intake.scanner,
            retention_seconds=self.intake.retention_seconds,
            clock=self.intake.store.clock,
            read_policy=ArtifactReadPolicy.CREATOR,
        )
        try:
            return artifacts.put_bytes(
                task_id=artifact.task_id,
                artifact_type=_ARTIFACT_TYPE,
                content=content,
                created_by=actor.user_id,
                extension="json",
            )
        except (OSError, sqlite3.Error) as exc:
            raise XlsxIntakeUnavailable("xlsx_analysis_store_unavailable") from exc

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
        artifact = self._write(projected, actor)
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
        current, _, after = self._current(file_id, actor, token, request.snapshot_hash, deadline)
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
    "BindXlsxAnalysis",
    "XlsxAnalysisArtifact",
    "XlsxAnalysisBinder",
    "XlsxAnalysisColumn",
    "XlsxAnalysisDataset",
    "XlsxAnalysisRefused",
    "load_xlsx_analysis_artifact",
]
