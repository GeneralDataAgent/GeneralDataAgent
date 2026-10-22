"""Target-free, immutable analysis snapshots with explicit derivation lineage."""

import json
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from generaldataagent.artifacts import LocalArtifactStore
from generaldataagent.contracts import ArtifactRef, SnapshotColumn
from generaldataagent.contracts.models import ContractModel
from generaldataagent.contracts.statistics import DerivedFeature
from generaldataagent.data_science.snapshot import (
    _validate_declared_types,
    canonical_snapshot_bytes,
)
from generaldataagent.data_science.statistics import derive_features


class AnalysisSnapshot(ContractModel):
    snapshot_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    tenant_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    data_domain: str = Field(min_length=1)
    source_name: str = Field(min_length=1)
    row_count: int = Field(ge=2)
    columns: tuple[SnapshotColumn, ...] = Field(min_length=1)
    data_artifact: ArtifactRef
    source_snapshot_id: UUID | None = None
    features: tuple[DerivedFeature, ...] = ()
    predictive_safe: bool = True

    @model_validator(mode="after")
    def coherent(self):
        if self.task_id != self.data_artifact.task_id:
            raise ValueError("analysis artifact task mismatch")
        if len({c.name for c in self.columns}) != len(self.columns):
            raise ValueError("analysis column names must be unique")
        if self.source_snapshot_id is None and self.features:
            raise ValueError("derived features require parent snapshot")
        if self.predictive_safe and any(f.group_by for f in self.features):
            raise ValueError("full-population group features cannot be predictive inputs")
        return self


def create_analysis_snapshot(*, rows, columns, artifact_store, **metadata) -> AnalysisSnapshot:
    content = canonical_snapshot_bytes(rows)
    if tuple(rows[0]) != tuple(c.name for c in columns):
        raise ValueError("analysis schema does not match rows")
    _validate_declared_types(rows, columns)
    artifact = artifact_store.put_bytes(
        task_id=metadata["task_id"],
        artifact_type="analysis_snapshot",
        content=content,
        created_by="data_scientist",
        extension="json",
    )
    return AnalysisSnapshot(
        row_count=len(rows), columns=columns, data_artifact=artifact, **metadata
    )


def load_analysis_rows(snapshot: AnalysisSnapshot, artifact_store: LocalArtifactStore):
    content = artifact_store.read_bytes(snapshot.data_artifact)
    rows = json.loads(content)
    if len(rows) != snapshot.row_count or canonical_snapshot_bytes(rows) != content:
        raise ValueError("analysis snapshot manifest mismatch")
    if tuple(rows[0]) != tuple(c.name for c in snapshot.columns):
        raise ValueError("analysis snapshot schema mismatch")
    _validate_declared_types(rows, snapshot.columns)
    return rows


def derive_analysis_snapshot(*, snapshot, features, artifact_store) -> AnalysisSnapshot:
    rows = derive_features(load_analysis_rows(snapshot, artifact_store), features)
    columns = snapshot.columns + tuple(
        SnapshotColumn(
            name=f.name,
            data_type="number",
            role="feature",
        )
        for f in features
    )
    artifact = artifact_store.put_bytes(
        task_id=snapshot.task_id,
        artifact_type="derived_analysis_snapshot",
        content=canonical_snapshot_bytes(rows),
        created_by="data_scientist",
        lineage=(snapshot.data_artifact.artifact_id,),
        extension="json",
    )
    return AnalysisSnapshot(
        task_id=snapshot.task_id,
        tenant_id=snapshot.tenant_id,
        project_id=snapshot.project_id,
        data_domain=snapshot.data_domain,
        source_name=snapshot.source_name,
        row_count=len(rows),
        columns=columns,
        data_artifact=artifact,
        source_snapshot_id=snapshot.snapshot_id,
        features=snapshot.features + tuple(features),
        predictive_safe=snapshot.predictive_safe and not any(f.group_by for f in features),
    )


def promote_analysis_snapshot(
    *, snapshot, target, features, artifact_store, group_column=None, timestamp_column=None
):
    """Bind a real target for prediction without full-population fitted features."""
    from generaldataagent.data_science.snapshot import create_dataset_snapshot

    if not snapshot.predictive_safe:
        raise ValueError("full-population aggregate lineage is not predictive safe")
    contaminated = {target}
    for feature in snapshot.features:
        if contaminated.intersection(feature.columns):
            contaminated.add(feature.name)
    if contaminated.intersection(features):
        raise ValueError("derived target leakage")
    roles = {name: "feature" for name in features}
    roles[target] = "target"
    if group_column:
        roles[group_column] = "group"
    if timestamp_column:
        roles[timestamp_column] = "timestamp"
    if len(roles) != len(features) + 1 + bool(group_column) + bool(timestamp_column):
        raise ValueError("predictive column roles must be disjoint")
    schema = {c.name: c for c in snapshot.columns}
    if not set(roles) <= set(schema):
        raise ValueError("unknown predictive columns")
    columns = tuple(schema[name].model_copy(update={"role": role}) for name, role in roles.items())
    rows = [
        {name: row[name] for name in roles} for row in load_analysis_rows(snapshot, artifact_store)
    ]
    return create_dataset_snapshot(
        task_id=snapshot.task_id,
        tenant_id=snapshot.tenant_id,
        project_id=snapshot.project_id,
        data_domain=snapshot.data_domain,
        source_name=snapshot.source_name,
        rows=rows,
        columns=columns,
        artifact_store=artifact_store,
        lineage=(snapshot.data_artifact.artifact_id,),
    )
