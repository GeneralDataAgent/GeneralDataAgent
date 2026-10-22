"""Immutable, canonical dataset snapshots for experiments."""

import json
import math
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.contracts import DatasetSnapshot, SnapshotColumn


def canonical_snapshot_bytes(rows: Sequence[dict[str, Any]]) -> bytes:
    if len(rows) < 2:
        raise ValueError("dataset snapshot requires at least two rows")
    columns = tuple(rows[0])
    if not columns:
        raise ValueError("dataset snapshot cannot have empty rows")
    for index, row in enumerate(rows):
        if tuple(row) != columns:
            raise ValueError(f"snapshot row {index} does not match the declared column order")
        for value in row.values():
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("snapshot values must be finite")
            if value is not None and not isinstance(value, (bool, int, float, str)):
                raise ValueError("snapshot values must be scalar JSON values")
    return json.dumps(
        list(rows), ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode()


def create_dataset_snapshot(
    *,
    task_id: UUID,
    tenant_id: str,
    project_id: str,
    data_domain: str,
    source_name: str,
    rows: Sequence[dict[str, Any]],
    columns: tuple[SnapshotColumn, ...],
    artifact_store: LocalArtifactStore,
    lineage: tuple[UUID, ...] = (),
) -> DatasetSnapshot:
    declared = tuple(column.name for column in columns)
    if tuple(rows[0]) != declared:
        raise ValueError("snapshot columns must match row keys and order")
    _validate_declared_types(rows, columns)
    content = canonical_snapshot_bytes(rows)
    artifact = artifact_store.put_bytes(
        task_id=task_id,
        artifact_type="dataset_snapshot",
        content=content,
        created_by="data_scientist",
        extension="json",
        lineage=lineage,
    )
    return DatasetSnapshot(
        task_id=task_id,
        tenant_id=tenant_id,
        project_id=project_id,
        data_domain=data_domain,
        source_name=source_name,
        content_hash=artifact.content_hash,
        row_count=len(rows),
        columns=columns,
        data_artifact=artifact,
    )


def load_snapshot_rows(
    snapshot: DatasetSnapshot, artifact_store: LocalArtifactStore
) -> list[dict[str, Any]]:
    content = artifact_store.read_bytes(snapshot.data_artifact)
    rows = json.loads(content)
    if not isinstance(rows, list) or len(rows) != snapshot.row_count:
        raise ValueError("snapshot artifact does not match its manifest")
    if canonical_snapshot_bytes(rows) != content:
        raise ValueError("snapshot artifact is not canonical")
    return rows


def _validate_declared_types(
    rows: Sequence[dict[str, Any]], columns: tuple[SnapshotColumn, ...]
) -> None:
    validators = {
        "boolean": lambda value: isinstance(value, bool),
        "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
        "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
        "string": lambda value: isinstance(value, str),
    }
    for row_index, row in enumerate(rows):
        for column in columns:
            value = row[column.name]
            if value is not None and not validators[column.data_type](value):
                raise ValueError(
                    f"snapshot row {row_index} column {column.name!r} does not match "
                    f"declared type {column.data_type}"
                )
