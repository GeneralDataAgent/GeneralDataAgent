"""Opt-in private XLSX staging with a pinned, unconfirmed text preview."""

import hashlib
import json
import platform
import re
import unicodedata
from collections.abc import Callable
from datetime import timedelta
from importlib import import_module
from pathlib import Path
from time import monotonic
from uuid import UUID, uuid4

from generaldataagent.contracts import ActorContext, AuditEvent
from generaldataagent.datasets.xlsx_intake_contracts import (
    ConfirmXlsxText,
    XlsxFilePreview,
    XlsxFileRecord,
    XlsxIntakeError,
    XlsxIntakeLimit,
    XlsxLimits,
    XlsxTextConfig,
    _XlsxPayload,
    confirmation_digest,
)
from generaldataagent.datasets.xlsx_intake_store import (
    XlsxIntakeConflict,
    XlsxIntakeUnavailable,
    XlsxUploadStore,
    preview_hash,
)
from generaldataagent.datasets.xlsx_preview import preview_xlsx
from generaldataagent.memory.display_preferences import canonical_hash
from generaldataagent.orchestration.task_queue import canonical_actor
from generaldataagent.security.dlp import TextDLPScanner
from generaldataagent.security.identity import SessionBroker

_MODULE_HASHES = {
    name: hashlib.sha256(Path(import_module(name).__file__).read_bytes()).hexdigest()
    for name in (
        "generaldataagent.datasets.xlsx_preflight",
        "generaldataagent.datasets.xlsx_preview",
    )
}
_PROCESSING_FINGERPRINT = canonical_hash(
    {
        "stage": "xlsx-private-preview-v1",
        "intake_source": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
)


def parser_fingerprint(limits: XlsxLimits, config: XlsxTextConfig) -> str:
    return canonical_hash(
        {
            "modules": _MODULE_HASHES,
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "config": config.model_dump(mode="json"),
            "limits": limits.model_dump(mode="json"),
        }
    )


class XlsxIntakeService:
    def __init__(
        self,
        *,
        store: XlsxUploadStore,
        broker: SessionBroker,
        audit,
        scanner: TextDLPScanner | None = None,
        limits: XlsxLimits | None = None,
        retention_seconds: int = 86400,
        monotonic_clock: Callable[[], float] = monotonic,
    ):
        if not callable(getattr(audit, "emit", None)):
            raise ValueError("xlsx_intake_requires_audit")
        if type(retention_seconds) is not int or not 1 <= retention_seconds <= 86400:
            raise ValueError("invalid_xlsx_retention")
        self.store, self.broker, self.audit = store, broker, audit
        self.scanner, self.limits = scanner or TextDLPScanner(), limits or XlsxLimits()
        self.retention_seconds, self.monotonic = retention_seconds, monotonic_clock

    def guard(self, actor: ActorContext, token: str, *, deadline: float | None = None):
        reference = self.broker.session_reference(token)
        end = self.monotonic() + self.limits.max_seconds
        if deadline is not None:
            end = min(end, deadline)

        def check():
            if self.monotonic() >= end:
                raise XlsxIntakeLimit("xlsx_operation_time_limit")
            identity = self.broker.authenticate_reference(reference)
            if (
                identity.business_constraint is not None
                or "analysis:run" not in identity.permissions
                or (identity.user_id, identity.tenant_id, identity.project_id)
                != (actor.user_id, actor.tenant_id, actor.project_id)
                or not set(actor.data_domains) <= identity.data_domains
            ):
                raise PermissionError("xlsx_authorization_unavailable")

        check()
        return check

    def _bound_guard(self, payload, check):
        def bound():
            check()
            if payload.file.expires_at <= self.store.now():
                raise PermissionError("xlsx_file_unavailable")

        bound()
        return bound

    def _audit(self, kind, payload):
        try:
            self.audit.emit(
                AuditEvent(
                    event_type=kind,
                    task_id=payload.file.task_id,
                    actor_id=payload.actor.user_id,
                    tenant_id=payload.actor.tenant_id,
                    trace_id=str(payload.file.task_id),
                    occurred_at=self.store.now(),
                    payload={
                        "file_id": str(payload.file.file_id),
                        "source_hash": payload.file.source_hash,
                        "preview_hash": payload.file.preview_hash,
                    },
                )
            )
        except Exception as exc:
            raise XlsxIntakeUnavailable("xlsx_audit_unavailable") from exc

    def _scan(self, preview, check):
        """Scan only the preview text; no claim that non-previewed cells were inspected."""
        check()
        sensitive = {
            "password",
            "apikey",
            "accesstoken",
            "secret",
            "clientsecret",
            "authorization",
            "privatekey",
        }
        if any(
            re.sub(r"[\s_-]+", "", unicodedata.normalize("NFKC", column).casefold()) in sensitive
            for column in preview.header
        ):
            raise PermissionError("xlsx_credential_column_denied")
        semantic = {
            "sheet": preview.sheet,
            "header": list(preview.header),
            "rows": [list(row) for row in preview.rows],
            "formulas": [
                {"formula": item.formula, "cached_text": item.cached_text}
                for item in preview.formulas
            ],
        }
        self.scanner.check(json.dumps(semantic, ensure_ascii=False).encode())
        check()

    def stage(
        self,
        raw: bytes,
        actor: ActorContext,
        token: str,
        idempotency_key: str,
        *,
        sheet: str | None = None,
        deadline=None,
    ):
        actor = canonical_actor(actor)
        check = self.guard(actor, token, deadline=deadline)
        if type(raw) is not bytes or not raw:
            raise XlsxIntakeError("xlsx_input_empty")
        if len(raw) > self.limits.max_bytes:
            raise XlsxIntakeLimit("xlsx_request_byte_limit")
        result = preview_xlsx(raw, sheet=sheet)
        self._scan(result.data, check)
        config = XlsxTextConfig(sheet=result.data.sheet)
        fingerprint = parser_fingerprint(self.limits, config)
        source_hash = result.preflight.content_sha256
        now = self.store.now()
        record = XlsxFileRecord(
            file_id=uuid4(),
            task_id=uuid4(),
            source_hash=source_hash,
            parser_fingerprint=fingerprint,
            processing_fingerprint=_PROCESSING_FINGERPRINT,
            config=config,
            config_hash=canonical_hash(config.model_dump(mode="json")),
            preflight_digest=canonical_hash(result.preflight.model_dump(mode="json")),
            preview_hash=preview_hash(
                source_hash,
                fingerprint,
                _PROCESSING_FINGERPRINT,
                result.preflight,
                result.data,
            ),
            byte_count=len(raw),
            created_at=now,
            expires_at=now + timedelta(seconds=self.retention_seconds),
        )
        payload = _XlsxPayload(
            actor=actor,
            file=record,
            preview=result.data,
            preflight=result.preflight,
        )
        _, payload = self.store.stage(
            payload,
            raw,
            idempotency_key,
            check,
            lambda: self._audit("xlsx.stage_intent", payload),
        )
        check()
        return self._preview(payload)

    @staticmethod
    def _preview(payload):
        return XlsxFilePreview(
            file=payload.file,
            preflight=payload.preflight,
            data=payload.preview,
        )

    def _parsed(self, payload, raw, check):
        if (
            payload.file.parser_fingerprint != parser_fingerprint(self.limits, payload.file.config)
            or payload.file.processing_fingerprint != _PROCESSING_FINGERPRINT
        ):
            raise XlsxIntakeConflict("xlsx_parser_or_limits_changed_restage_required")
        result = preview_xlsx(raw, sheet=payload.file.config.sheet)
        if result.preflight != payload.preflight or result.data != payload.preview:
            raise PermissionError("xlsx_preview_binding_unavailable")
        self._scan(payload.preview, check)
        return result

    def preview(self, file_id: UUID, actor: ActorContext, token: str):
        check = self.guard(actor, token)
        with self.store.db() as db:
            _, payload, raw = self.store.owned(db, file_id, actor)
            bound = self._bound_guard(payload, check)
            self._parsed(payload, raw, bound)
            bound()
            return self._preview(payload)

    def confirm(
        self,
        file_id: UUID,
        actor: ActorContext,
        token: str,
        request: ConfirmXlsxText,
        *,
        deadline=None,
    ):
        request = ConfirmXlsxText.model_validate(request.model_dump())
        check = self.guard(actor, token, deadline=deadline)
        with self.store.db(write=True) as db:
            row, payload, raw = self.store.owned(db, file_id, actor)
            bound = self._bound_guard(payload, check)
            if (
                request.source_hash != payload.file.source_hash
                or request.preview_hash != payload.file.preview_hash
            ):
                raise XlsxIntakeConflict("xlsx_confirmation_source_or_preview_changed")
            self._parsed(payload, raw, bound)
            if payload.file.status == "confirmed":
                bound()
                return self._preview(payload)
            record = payload.file.model_copy(
                update={
                    "status": "confirmed",
                    "confirmation_hash": confirmation_digest(payload.file),
                    "confirmed_at": self.store.now(),
                }
            )
            confirmed = payload.model_copy(update={"file": record})
            encrypted = self.store.seal(
                confirmed.model_dump_json().encode(), self.store.body_aad(row)
            )
            self._audit("xlsx.confirm_intent", payload)
            db.execute("UPDATE xlsx_uploads SET body=? WHERE file_id=?", (encrypted, str(file_id)))
            bound()
            return self._preview(confirmed)
