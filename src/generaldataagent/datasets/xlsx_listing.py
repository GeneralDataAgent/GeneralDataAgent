"""Creator-private metadata discovery over the retained XLSX staging inventory.

Listing never parses a workbook, re-previews it, or returns headers/values. An encrypted cursor
freezes membership and order, while each page rechecks current authorization and expiry.
"""

import base64
import re
from datetime import timedelta
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, model_validator

from generaldataagent.contracts import ActorContext, AuditEvent
from generaldataagent.contracts.models import ContractModel
from generaldataagent.datasets import xlsx_intake
from generaldataagent.datasets.xlsx_intake import XlsxIntakeService
from generaldataagent.datasets.xlsx_intake_store import XlsxIntakeConflict, XlsxIntakeUnavailable
from generaldataagent.memory.display_preferences import canonical_hash
from generaldataagent.orchestration.task_queue import canonical_actor


class XlsxFileSummary(ContractModel):
    """Metadata only: no headers, cell values, formulas, hashes or idempotency keys."""

    schema_version: Literal["1.0"] = "1.0"
    file_id: UUID
    task_id: UUID
    status: Literal["staged", "confirmed"]
    availability: Literal["compatible", "release_incompatible"]
    sheet: str = Field(min_length=1, max_length=31)
    row_count: int = Field(ge=1)
    column_count: int = Field(ge=0)
    byte_count: int = Field(ge=1)
    has_interpretation: bool
    created_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def valid_lifetime(self):
        if self.expires_at <= self.created_at:
            raise ValueError("xlsx_summary_lifetime_invalid")
        return self


class XlsxFilePage(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    items: tuple[XlsxFileSummary, ...] = Field(max_length=20)
    next_cursor: str | None = Field(default=None, min_length=1, max_length=4096)


class _Cursor(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    remaining: tuple[UUID, ...] = Field(min_length=1, max_length=20)
    issued_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def valid_snapshot(self):
        lifetime = self.expires_at - self.issued_at
        if len(set(self.remaining)) != len(self.remaining) or not (
            timedelta(0) < lifetime <= timedelta(minutes=5)
        ):
            raise ValueError("xlsx_cursor_invalid")
        return self


class XlsxFileListing:
    def __init__(self, intake: XlsxIntakeService):
        self.intake = intake

    def list_files(
        self, actor: ActorContext, token: str, *, limit: int = 10, cursor: str | None = None
    ) -> XlsxFilePage:
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("xlsx_page_limit_invalid")
        actor = canonical_actor(actor)
        port, store = self.intake, self.intake.store
        check = port.guard(actor, token)
        epoch = canonical_hash(
            {
                "limits": port.limits.model_dump(mode="json"),
                "processing": xlsx_intake._PROCESSING_FINGERPRINT,
            }
        )
        # AAD binds identity, exact domains, page size and listing release; never a session.
        aad = f"xlsx-list-cursor-v1:{store.scope(actor)}:{limit}:{epoch}".encode()
        now = store.now()
        snapshot = None
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or not re.fullmatch(
                    r"[A-Za-z0-9_-]{1,4096}", cursor
                ):
                    raise ValueError("encoding")
                encrypted = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
                if base64.urlsafe_b64encode(encrypted).decode().rstrip("=") != cursor:
                    raise ValueError("noncanonical")
                snapshot = _Cursor.model_validate_json(store.open(encrypted, aad))
                if not snapshot.issued_at <= now < snapshot.expires_at:
                    raise ValueError("expired")
            except Exception as exc:
                raise XlsxIntakeConflict("xlsx_list_cursor_invalid_or_expired") from exc
        with store.db() as db:
            # Hard retained ceiling, independent of a later tightened admission quota.
            rows = db.execute(
                "SELECT * FROM xlsx_uploads WHERE scope=? LIMIT 21", (store.scope(actor),)
            ).fetchall()
            if len(rows) > 20:
                raise XlsxIntakeUnavailable("xlsx_inventory_capacity_invalid")
            inventory = {}
            for row in rows:
                check()
                payload, _ = store.decode(row)
                if payload.actor != actor:
                    raise PermissionError("xlsx_file_unavailable")
                record = payload.file
                if record.expires_at <= store.now():
                    continue
                inventory[record.file_id] = XlsxFileSummary(
                    file_id=record.file_id,
                    task_id=record.task_id,
                    status=record.status,
                    availability=(
                        "compatible"
                        if record.parser_fingerprint
                        == xlsx_intake.parser_fingerprint(port.limits, record.config)
                        and record.processing_fingerprint == xlsx_intake._PROCESSING_FINGERPRINT
                        else "release_incompatible"
                    ),
                    sheet=record.config.sheet,
                    row_count=payload.preview.row_count,
                    column_count=payload.preview.column_count,
                    byte_count=record.byte_count,
                    has_interpretation=payload.interpretation is not None,
                    created_at=record.created_at,
                    expires_at=record.expires_at,
                )
        if snapshot is None:
            ordered = sorted(inventory, key=lambda k: (inventory[k].created_at, str(k)))
            issued_at, expires_at = now, now + timedelta(minutes=5)
        else:
            ordered = list(snapshot.remaining)
            issued_at, expires_at = snapshot.issued_at, snapshot.expires_at
        live = [k for k in ordered if k in inventory and inventory[k].expires_at > store.now()]
        selected, remaining = live[:limit], live[limit:]
        next_cursor = None
        if remaining:
            body = _Cursor(remaining=remaining, issued_at=issued_at, expires_at=expires_at)
            next_cursor = base64.urlsafe_b64encode(store.seal(body.model_dump_json().encode(), aad))
            next_cursor = next_cursor.decode().rstrip("=")
        check()
        try:
            port.audit.emit(
                AuditEvent(
                    event_type="xlsx.files_listed",
                    task_id=uuid4(),
                    actor_id=actor.user_id,
                    tenant_id=actor.tenant_id,
                    trace_id=str(uuid4()),
                    occurred_at=store.now(),
                    payload={"item_count": len(selected), "has_more": next_cursor is not None},
                )
            )
        except Exception as exc:
            raise XlsxIntakeUnavailable("xlsx_audit_unavailable") from exc
        check()
        if not issued_at <= store.now() < expires_at:
            raise XlsxIntakeConflict("xlsx_list_cursor_invalid_or_expired")
        return XlsxFilePage(
            items=tuple(inventory[k] for k in selected if inventory[k].expires_at > store.now()),
            next_cursor=next_cursor,
        )
