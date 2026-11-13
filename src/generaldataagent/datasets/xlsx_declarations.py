"""Creator-private listing of declared XLSX intent, without headers, tokens or values.

A declaration is stored inside the encrypted intake payload. This listing reports only its binding
facts — header row, per-column index/type/missing policy, duplicate policy, interpretation hash and
snapshot state — for the caller's current, unexpired files. It never parses a workbook, re-derives
readings, returns header labels or missing tokens, or changes any row.
"""

from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, model_validator

from generaldataagent.contracts import ActorContext, AuditEvent
from generaldataagent.contracts.models import ContractModel
from generaldataagent.datasets import xlsx_intake
from generaldataagent.datasets.xlsx_intake import XlsxIntakeService
from generaldataagent.datasets.xlsx_intake_contracts import (
    MAX_MISSING_TOKENS,
    DeclaredColumnType,
)
from generaldataagent.datasets.xlsx_intake_store import XlsxIntakeUnavailable
from generaldataagent.datasets.xlsx_preflight import MAX_ROWS
from generaldataagent.datasets.xlsx_preview import PREVIEW_COLUMNS
from generaldataagent.orchestration.task_queue import canonical_actor


class XlsxDeclaredColumn(ContractModel):
    """One declared column, without its header label or missing-value tokens."""

    schema_version: Literal["1.0"] = "1.0"
    index: int = Field(ge=0, lt=PREVIEW_COLUMNS, strict=True)
    type: DeclaredColumnType
    empty_is_missing: bool
    missing_token_count: int = Field(ge=0, le=MAX_MISSING_TOKENS, strict=True)


class XlsxDeclarationSummary(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    file_id: UUID
    task_id: UUID
    status: Literal["confirmed"] = "confirmed"
    availability: Literal["compatible", "release_incompatible"]
    sheet: str = Field(min_length=1, max_length=31)
    header_row: int = Field(ge=1, le=MAX_ROWS, strict=True)
    columns: tuple[XlsxDeclaredColumn, ...] = Field(max_length=PREVIEW_COLUMNS)
    duplicate_rows: Literal["reject", "keep-all"]
    interpretation_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    snapshot_state: Literal["none", "current", "stale"]
    created_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def summary_is_coherent(self) -> "XlsxDeclarationSummary":
        indices = [column.index for column in self.columns]
        if any(indices[index + 1] <= indices[index] for index in range(len(indices) - 1)):
            raise ValueError("xlsx_declaration_column_order_invalid")
        if self.expires_at <= self.created_at:
            raise ValueError("xlsx_declaration_lifetime_invalid")
        return self


class XlsxDeclarationPage(ContractModel):
    """Bounded declaration metadata; never header text, tokens, values or formulas."""

    schema_version: Literal["1.0"] = "1.0"
    items: tuple[XlsxDeclarationSummary, ...] = Field(max_length=20)
    declarations: Literal["metadata-only"] = "metadata-only"
    headers: Literal["not-returned"] = "not-returned"
    tokens: Literal["counts-only"] = "counts-only"
    values: Literal["not-returned"] = "not-returned"

    @model_validator(mode="after")
    def file_ids_are_unique(self) -> "XlsxDeclarationPage":
        file_ids = [item.file_id for item in self.items]
        if len(set(file_ids)) != len(file_ids):
            raise ValueError("xlsx_declaration_duplicate")
        return self


class XlsxDeclarationListing:
    def __init__(self, intake: XlsxIntakeService):
        self.intake = intake

    def _summary(self, payload) -> XlsxDeclarationSummary:
        record = payload.file
        interpretation = payload.interpretation
        if interpretation is None or record.status != "confirmed":
            raise ValueError("xlsx_declaration_listing_requires_a_declaration")
        if payload.snapshot is None:
            snapshot_state = "none"
        elif payload.snapshot.interpretation_hash == interpretation.interpretation_hash:
            snapshot_state = "current"
        else:
            snapshot_state = "stale"
        return XlsxDeclarationSummary(
            file_id=record.file_id,
            task_id=record.task_id,
            availability=(
                "compatible"
                if record.parser_fingerprint
                == xlsx_intake.parser_fingerprint(self.intake.limits, record.config)
                and record.processing_fingerprint == xlsx_intake._PROCESSING_FINGERPRINT
                else "release_incompatible"
            ),
            sheet=record.config.sheet,
            header_row=interpretation.header_row,
            columns=tuple(
                XlsxDeclaredColumn(
                    index=column.index,
                    type=column.type,
                    empty_is_missing=column.empty_is_missing,
                    missing_token_count=len(column.missing_tokens),
                )
                for column in sorted(interpretation.columns, key=lambda item: item.index)
            ),
            duplicate_rows=interpretation.duplicate_rows,
            interpretation_hash=interpretation.interpretation_hash,
            snapshot_state=snapshot_state,
            created_at=record.created_at,
            expires_at=record.expires_at,
        )

    def list_declarations(self, actor: ActorContext, token: str) -> XlsxDeclarationPage:
        actor = canonical_actor(actor)
        port, store = self.intake, self.intake.store
        check = port.guard(actor, token)
        with store.db() as db:
            # Hard retained ceiling, independent of a later tightened admission quota.
            rows = db.execute(
                "SELECT * FROM xlsx_uploads WHERE scope=? LIMIT 21", (store.scope(actor),)
            ).fetchall()
            if len(rows) > 20:
                raise XlsxIntakeUnavailable("xlsx_inventory_capacity_invalid")
            now = store.now()
            summaries = []
            for row in rows:
                check()
                payload, _ = store.decode(row)
                if payload.actor != actor:
                    raise PermissionError("xlsx_file_unavailable")
                if payload.interpretation is None or payload.file.expires_at <= now:
                    continue
                summaries.append(self._summary(payload))
        items = tuple(sorted(summaries, key=lambda item: (item.created_at, str(item.file_id))))
        check()
        try:
            port.audit.emit(
                AuditEvent(
                    event_type="xlsx.declarations_listed",
                    task_id=uuid4(),
                    actor_id=actor.user_id,
                    tenant_id=actor.tenant_id,
                    trace_id=str(uuid4()),
                    occurred_at=store.now(),
                    payload={"item_count": len(items)},
                )
            )
        except Exception as exc:
            raise XlsxIntakeUnavailable("xlsx_audit_unavailable") from exc
        check()
        return XlsxDeclarationPage(items=items)
