"""Run fixed quality checks over a private retained XLSX snapshot."""

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Timer
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from generaldataagent.artifacts.governed import ArtifactReadPolicy, GovernedArtifactStore
from generaldataagent.connectors.sqlite import SQLiteConnector
from generaldataagent.contracts import ActorContext, ArtifactRef, Budget
from generaldataagent.contracts.data_engineering import QualityReport, QualityRule
from generaldataagent.contracts.models import ContractModel
from generaldataagent.data_engineering import DataEngineeringRuntime
from generaldataagent.datasets.xlsx_intake import XlsxIntakeService
from generaldataagent.datasets.xlsx_intake_contracts import (
    DeclaredColumnType,
    XlsxSnapshotColumnData,
    XlsxSnapshotData,
    _XlsxPayload,
)
from generaldataagent.datasets.xlsx_intake_store import XlsxIntakeConflict, XlsxIntakeUnavailable
from generaldataagent.datasets.xlsx_preflight import MAX_ROWS
from generaldataagent.datasets.xlsx_preview import PREVIEW_COLUMNS
from generaldataagent.policy.budgets import TaskBudgetLedger

SNAPSHOT_HASH_PATTERN = r"^sha256:[0-9a-f]{64}$"
_CONNECTOR = "xlsx_snapshot"
_RELATION = "declared_xlsx"
_EVIDENCE = "xlsx_snapshot_quality"


class XlsxQualityColumn(ContractModel):
    index: int = Field(ge=0, lt=PREVIEW_COLUMNS)
    type: DeclaredColumnType
    valid: int = Field(ge=0, le=MAX_ROWS)
    missing: int = Field(ge=0, le=MAX_ROWS)
    unparsed: int = Field(ge=0, le=MAX_ROWS)
    untrusted: int = Field(ge=0, le=MAX_ROWS)


class XlsxQualitySource(ContractModel):
    file_id: UUID
    source_task_id: UUID
    source_hash: str = Field(pattern=SNAPSHOT_HASH_PATTERN)
    preview_hash: str = Field(pattern=SNAPSHOT_HASH_PATTERN)
    confirmation_hash: str = Field(pattern=SNAPSHOT_HASH_PATTERN)
    interpretation_hash: str = Field(pattern=SNAPSHOT_HASH_PATTERN)
    snapshot_hash: str = Field(pattern=SNAPSHOT_HASH_PATTERN)
    data_rows: int = Field(ge=0, le=MAX_ROWS)
    duplicate_rows: Literal["reject", "keep-all"]
    duplicate_row_count: int = Field(ge=0, le=MAX_ROWS)
    columns: tuple[XlsxQualityColumn, ...] = Field(min_length=1, max_length=PREVIEW_COLUMNS)


def _column_name(index: int) -> str:
    return f"column_{index}"


class XlsxQualityAssessment(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    task_id: UUID
    source: XlsxQualitySource
    quality: QualityReport
    passed: bool
    relation_values: Literal["canonical-text-with-unusable-as-null"] = (
        "canonical-text-with-unusable-as-null"
    )

    @model_validator(mode="after")
    def report_matches_source(self) -> "XlsxQualityAssessment":
        source, quality = self.source, self.quality
        if (
            quality.connector_id != _CONNECTOR
            or quality.relation != _RELATION
            or quality.row_count != source.data_rows
            or len(quality.checks) != len(source.columns)
            or self.passed != _passed(source, quality)
        ):
            raise ValueError("xlsx_quality_report_binding_invalid")
        for column, check in zip(source.columns, quality.checks, strict=True):
            name = _column_name(column.index)
            invalid = column.missing + column.unparsed + column.untrusted
            if (
                column.valid + invalid != source.data_rows
                or check.rule_id != f"not_null_{name}"
                or check.column != name
                or check.rule_type != "not_null"
                or check.severity != "error"
                or check.violation_count != invalid
                or check.passed != (invalid == 0)
            ):
                raise ValueError("xlsx_quality_report_binding_invalid")
        return self


class XlsxQualityResult(ContractModel):
    assessment: XlsxQualityAssessment
    evidence: ArtifactRef

    @model_validator(mode="after")
    def evidence_belongs_to_analysis(self) -> "XlsxQualityResult":
        if self.evidence.task_id != self.assessment.task_id or self.evidence.artifact_type != (
            _EVIDENCE
        ):
            raise ValueError("xlsx_quality_evidence_binding_invalid")
        return self


def _passed(source: XlsxQualitySource, quality: QualityReport) -> bool:
    return quality.passed and (
        source.duplicate_rows == "keep-all" or not source.duplicate_row_count
    )


class _QualityArtifactStore(GovernedArtifactStore):
    """Put the actual runtime report and its source binding in one governed artifact."""

    def __init__(self, *args, source: XlsxQualitySource, **kwargs):
        super().__init__(*args, **kwargs)
        self.source = source
        self.ref: ArtifactRef | None = None

    def put_json(self, *, task_id, artifact_type, value, **kwargs):
        if artifact_type != "quality_report" or self.ref is not None:
            raise ValueError("xlsx_quality_unexpected_artifact")
        quality = QualityReport.model_validate(value)
        assessment = XlsxQualityAssessment(
            task_id=task_id,
            source=self.source,
            quality=quality,
            passed=_passed(self.source, quality),
        )
        self.ref = super().put_json(
            task_id=task_id,
            artifact_type=_EVIDENCE,
            value=assessment.model_dump(mode="json"),
            **kwargs,
        )
        return self.ref


def _relation_rows(
    columns: tuple[XlsxSnapshotColumnData, ...], count: int, check: Callable[[], None]
) -> Iterator[tuple[str | None, ...]]:
    values = tuple(iter(column.values) for column in columns)
    for position in range(count):
        if position % 256 == 0:
            check()
        yield tuple(
            next(data) if column.statuses[position] == "v" else None
            for column, data in zip(columns, values, strict=True)
        )


class XlsxQualityService:
    def __init__(
        self,
        *,
        intake: XlsxIntakeService,
        budgets: TaskBudgetLedger,
        artifact_root: Path,
        artifact_index: Path,
        scratch_root: Path,
    ):
        if any(
            path.resolve().is_relative_to(artifact_root.resolve())
            for path in (intake.store.path, budgets.path, artifact_index, scratch_root)
        ):
            raise ValueError("xlsx_state_and_scratch_must_be_outside_artifact_content")
        self.intake, self.budgets = intake, budgets
        self.artifact_root, self.artifact_index, self.scratch_root = (
            artifact_root,
            artifact_index,
            scratch_root,
        )
        scratch_root.mkdir(parents=True, exist_ok=True)

    def _snapshot(
        self,
        file_id: UUID,
        actor: ActorContext,
        token: str,
        snapshot_hash: str,
        deadline: float,
        *,
        reparse: bool = False,
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
                or snapshot.snapshot_hash != snapshot_hash
            ):
                raise XlsxIntakeConflict("xlsx_snapshot_changed")
            if reparse:
                self.intake._parsed(payload, raw, bound)
            bound()
            return payload, snapshot, bound

    @staticmethod
    def _source(payload: _XlsxPayload) -> XlsxQualitySource:
        snapshot = payload.snapshot
        assert snapshot is not None
        return XlsxQualitySource(
            file_id=payload.file.file_id,
            source_task_id=payload.file.task_id,
            source_hash=snapshot.source_hash,
            preview_hash=snapshot.preview_hash,
            confirmation_hash=snapshot.confirmation_hash,
            interpretation_hash=snapshot.interpretation_hash,
            snapshot_hash=snapshot.snapshot_hash,
            data_rows=len(snapshot.rows),
            duplicate_rows=snapshot.duplicate_rows,
            duplicate_row_count=snapshot.duplicate_row_count,
            columns=tuple(
                XlsxQualityColumn(
                    index=c.index,
                    type=c.type,
                    valid=len(c.values),
                    missing=c.statuses.count("m"),
                    unparsed=c.statuses.count("u"),
                    untrusted=c.statuses.count("t"),
                )
                for c in snapshot.columns
            ),
        )

    def _artifacts(self, actor: ActorContext, source: XlsxQualitySource) -> _QualityArtifactStore:
        return _QualityArtifactStore(
            self.artifact_root,
            index_path=self.artifact_index,
            actor=actor,
            scanner=self.intake.scanner,
            read_policy=ArtifactReadPolicy.CREATOR,
            retention_seconds=self.intake.retention_seconds,
            clock=self.intake.store.clock,
            source=source,
        )

    @staticmethod
    def _read_evidence(artifacts: _QualityArtifactStore, ref: ArtifactRef) -> XlsxQualityResult:
        if (
            ref.artifact_type != _EVIDENCE
            or ref.uri != f"artifact://local/{ref.task_id}/{ref.artifact_id}.json"
        ):
            raise XlsxIntakeConflict("xlsx_quality_evidence_binding_invalid")
        try:
            content = artifacts.export_bytes(ref)
            assessment = XlsxQualityAssessment.model_validate_json(content)
        except PermissionError:
            raise
        except (OSError, ValueError) as exc:
            raise XlsxIntakeConflict("xlsx_quality_evidence_unavailable") from exc
        if (
            len(content) != ref.size_bytes
            or assessment.source != artifacts.source
            or assessment.task_id != ref.task_id
        ):
            raise XlsxIntakeConflict("xlsx_quality_evidence_binding_invalid")
        return XlsxQualityResult(assessment=assessment, evidence=ref)

    def assess(
        self, file_id: UUID, actor: ActorContext, token: str, snapshot_hash: str
    ) -> XlsxQualityResult:
        end = self.intake.monotonic() + self.intake.limits.max_seconds
        payload, snapshot, check = self._snapshot(
            file_id, actor, token, snapshot_hash, end, reparse=True
        )
        actor = payload.actor
        source = self._source(payload)
        artifacts = self._artifacts(actor, source)
        task_id = uuid4()
        self.budgets.register(
            task_id,
            actor,
            Budget(max_runtime_seconds=self.intake.limits.max_seconds, max_tool_calls=1),
        )
        names = tuple(_column_name(c.index) for c in snapshot.columns)
        try:
            with TemporaryDirectory(prefix="xlsx-quality-", dir=self.scratch_root) as directory:
                database = Path(directory) / "declared.db"
                definitions = ", ".join(f"{name} TEXT" for name in names)
                placeholders = ", ".join("?" for _ in names)
                with closing(sqlite3.connect(database)) as relation, relation:
                    relation.execute(f"CREATE TABLE {_RELATION} ({definitions})")
                    relation.executemany(
                        f"INSERT INTO {_RELATION} VALUES ({placeholders})",
                        _relation_rows(snapshot.columns, source.data_rows, check),
                    )
                check()
                remaining = max(0.001, end - self.intake.monotonic())
                runtime = DataEngineeringRuntime(
                    connectors={
                        _CONNECTOR: SQLiteConnector(
                            database,
                            connector_id=_CONNECTOR,
                            timeout_seconds=remaining,
                        )
                    },
                    connector_domains={_CONNECTOR: frozenset(actor.data_domains)},
                    artifacts=artifacts,
                    audit=self.intake.audit,
                )
                runtime.gateway.budgets = self.budgets
                cancel = Event()
                timer = Timer(remaining, cancel.set)
                timer.daemon = True
                timer.start()
                try:
                    runtime.check_quality(
                        task_id=task_id,
                        actor=actor,
                        connector_id=_CONNECTOR,
                        relation=_RELATION,
                        rules=tuple(
                            QualityRule(
                                rule_id=f"not_null_{name}", column=name, rule_type="not_null"
                            )
                            for name in names
                        ),
                        trace_id=str(task_id),
                        cancel_event=cancel,
                    )
                finally:
                    timer.cancel()
            if artifacts.ref is None:
                raise XlsxIntakeUnavailable("xlsx_quality_evidence_not_written")
            result = self._read_evidence(artifacts, artifacts.ref)
            self.intake._audit(
                "xlsx.snapshot_quality_prepared",
                payload,
                {"snapshot_hash": snapshot_hash, "assessment_task_id": str(task_id)},
            )
            current, _, check = self._snapshot(file_id, actor, token, snapshot_hash, end)
            if self._source(current) != source:
                raise XlsxIntakeConflict("xlsx_snapshot_changed")
            check()
            return result
        except Exception:
            if artifacts.ref is not None:
                with suppress(Exception):
                    artifacts.discard(artifacts.ref)
            raise

    def verify(
        self,
        file_id: UUID,
        actor: ActorContext,
        token: str,
        snapshot_hash: str,
        ref: ArtifactRef,
        *,
        deadline: float,
    ) -> XlsxQualityResult:
        payload, _, check = self._snapshot(
            file_id, actor, token, snapshot_hash, deadline, reparse=True
        )
        artifacts = self._artifacts(payload.actor, self._source(payload))
        result = self._read_evidence(artifacts, ref)
        check()
        self.intake._audit(
            "xlsx.snapshot_quality_verified",
            payload,
            {"snapshot_hash": snapshot_hash, "assessment_task_id": str(ref.task_id)},
        )
        current, _, check = self._snapshot(file_id, actor, token, snapshot_hash, deadline)
        if self._source(current) != result.assessment.source:
            raise XlsxIntakeConflict("xlsx_snapshot_changed")
        check()
        return result
