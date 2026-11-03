"""Opt-in private CSV bytes -> confirmed text snapshot -> governed read-only profile."""

import base64
import hashlib
import json
import re
import sqlite3
import unicodedata
from collections.abc import Callable
from contextlib import closing, suppress
from datetime import timedelta
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Timer
from time import monotonic
from uuid import UUID, uuid4

from generaldataagent.artifacts.governed import ArtifactReadPolicy, GovernedArtifactStore
from generaldataagent.connectors.base import quote_identifier
from generaldataagent.connectors.sqlite import SQLiteConnector
from generaldataagent.contracts import ActorContext, AuditEvent, Budget
from generaldataagent.data_engineering import DataEngineeringRuntime
from generaldataagent.datasets import csv_parser
from generaldataagent.datasets.csv_intake_contracts import (
    ConfirmCsvText,
    CsvFilePreview,
    CsvFileRecord,
    CsvProfileProvenance,
    CsvProfileResult,
    _CsvPayload,
)
from generaldataagent.datasets.csv_intake_store import (
    CsvIntakeConflict,
    CsvIntakeUnavailable,
    CsvUploadStore,
    preview_hash,
    source_bytes,
)
from generaldataagent.datasets.csv_parser import (
    CsvIntakeLimit,
    CsvLimits,
    CsvTextConfig,
)
from generaldataagent.memory.display_preferences import canonical_hash
from generaldataagent.orchestration.task_queue import canonical_actor
from generaldataagent.policy.budgets import TaskBudgetLedger
from generaldataagent.security.dlp import TextDLPScanner
from generaldataagent.security.identity import SessionBroker

# Bind provenance to the process's release at import, not later mutable on-disk files.
_PROFILE_FINGERPRINT = canonical_hash(
    {
        "modules": {
            name: hashlib.sha256(Path(import_module(name).__file__).read_bytes()).hexdigest()
            for name in (
                "generaldataagent.data_engineering.runtime",
                "generaldataagent.data_engineering.profile",
                "generaldataagent.connectors.sqlite",
            )
        },
        "sqlite": sqlite3.sqlite_version,
        "intake_source": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
)


class _TrackedArtifacts(GovernedArtifactStore):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.created = []

    def put_bytes(self, **kwargs):
        ref = super().put_bytes(**kwargs)
        self.created.append(ref)
        return ref


class CsvIntakeService:
    def __init__(
        self,
        *,
        store: CsvUploadStore,
        broker: SessionBroker,
        budgets: TaskBudgetLedger,
        artifact_root: Path,
        artifact_index: Path,
        scratch_root: Path,
        audit,
        scanner: TextDLPScanner | None = None,
        limits: CsvLimits | None = None,
        retention_seconds: int = 86400,
        monotonic_clock: Callable[[], float] = monotonic,
    ):
        if not callable(getattr(audit, "emit", None)):
            raise ValueError("csv_intake_requires_audit")
        if type(retention_seconds) is not int or not 1 <= retention_seconds <= 86400:
            raise ValueError("invalid_csv_retention")
        if any(
            path.resolve().is_relative_to(artifact_root.resolve())
            for path in (store.path, artifact_index, scratch_root)
        ):
            raise ValueError("csv_state_and_scratch_must_be_outside_artifact_content")
        self.store, self.broker, self.budgets, self.audit = store, broker, budgets, audit
        self.artifact_root, self.artifact_index, self.scratch_root = (
            artifact_root,
            artifact_index,
            scratch_root,
        )
        self.scanner, self.limits = scanner or TextDLPScanner(), limits or CsvLimits()
        self.retention_seconds, self.monotonic = retention_seconds, monotonic_clock
        scratch_root.mkdir(parents=True, exist_ok=True)

    def guard(self, actor: ActorContext, token: str, *, deadline: float | None = None):
        reference = self.broker.session_reference(token)
        end = self.monotonic() + self.limits.max_seconds
        if deadline is not None:
            end = min(end, deadline)

        def check():
            if self.monotonic() >= end:
                raise CsvIntakeLimit("csv_operation_time_limit")
            identity = self.broker.authenticate_reference(reference)
            if (
                identity.business_constraint is not None
                or "analysis:run" not in identity.permissions
                or (identity.user_id, identity.tenant_id, identity.project_id)
                != (actor.user_id, actor.tenant_id, actor.project_id)
                or not set(actor.data_domains) <= identity.data_domains
            ):
                raise PermissionError("csv_authorization_unavailable")

        check()
        return check

    def _bound_guard(self, payload, check):
        def bound():
            check()
            if payload.file.expires_at <= self.store.now():
                raise PermissionError("csv_file_unavailable")

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
            raise CsvIntakeUnavailable("csv_audit_unavailable") from exc

    def _scan(self, raw, parsed, check):
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
            re.sub(r"[\s_-]+", "", unicodedata.normalize("NFKC", c).casefold()) in sensitive
            for c in parsed.preview.columns
        ):
            raise PermissionError("csv_credential_column_denied")
        self.scanner.check(raw)
        semantic = [dict(zip(parsed.preview.columns, row, strict=True)) for row in parsed.rows]
        self.scanner.check(json.dumps(semantic, ensure_ascii=False).encode())
        check()

    def stage(
        self, raw: bytes, actor: ActorContext, token: str, idempotency_key: str, *, deadline=None
    ):
        actor = canonical_actor(actor)
        check = self.guard(actor, token, deadline=deadline)
        parsed = csv_parser.parse_csv(raw, self.limits, check)
        self._scan(raw, parsed, check)
        fingerprint = csv_parser.parser_fingerprint(self.limits)
        source_hash = "sha256:" + hashlib.sha256(raw).hexdigest()
        now = self.store.now()
        config = CsvTextConfig()
        record = CsvFileRecord(
            file_id=uuid4(),
            task_id=uuid4(),
            source_hash=source_hash,
            config=config,
            config_hash=canonical_hash(config.model_dump(mode="json")),
            parser_fingerprint=fingerprint,
            processing_fingerprint=_PROFILE_FINGERPRINT,
            preview_hash=preview_hash(
                source_hash, fingerprint, parsed.preview, _PROFILE_FINGERPRINT
            ),
            status="staged",
            created_at=now,
            expires_at=now + timedelta(seconds=self.retention_seconds),
        )
        payload = _CsvPayload(
            actor=actor,
            file=record,
            preview=parsed.preview,
            source_base64=base64.b64encode(raw).decode(),
        )
        row, payload = self.store.stage(
            payload, idempotency_key, check, lambda: self._audit("csv.stage_intent", payload)
        )
        check()
        return self._preview(row, payload)

    @staticmethod
    def _preview(row, payload):
        record = payload.file.model_copy(
            update={"status": "profiled" if row["report"] is not None else "staged"}
        )
        return CsvFilePreview(file=record, data=payload.preview)

    def _parsed(self, payload, check):
        if (
            payload.file.parser_fingerprint != csv_parser.parser_fingerprint(self.limits)
            or payload.file.processing_fingerprint != _PROFILE_FINGERPRINT
        ):
            raise CsvIntakeConflict("csv_parser_or_limits_changed_restage_required")
        raw = source_bytes(payload)
        parsed = csv_parser.parse_csv(raw, self.limits, check)
        if parsed.preview != payload.preview:
            raise PermissionError("csv_preview_binding_unavailable")
        self._scan(raw, parsed, check)
        return parsed

    def preview(self, file_id: UUID, actor: ActorContext, token: str):
        check = self.guard(actor, token)
        with self.store.db() as db:
            row, payload = self.store.owned(db, file_id, actor)
            bound = self._bound_guard(payload, check)
            self._parsed(payload, bound)
            bound()
            return self._preview(row, payload)

    def _artifacts(self, actor):
        return _TrackedArtifacts(
            self.artifact_root,
            index_path=self.artifact_index,
            actor=actor,
            scanner=self.scanner,
            read_policy=ArtifactReadPolicy.CREATOR,
            retention_seconds=self.retention_seconds,
            clock=self.store.clock,
        )

    @staticmethod
    def _provenance(payload, fingerprint):
        record = payload.file
        return CsvProfileProvenance(
            file_id=record.file_id,
            task_id=record.task_id,
            source_hash=record.source_hash,
            config=record.config,
            config_hash=record.config_hash,
            parser_fingerprint=record.parser_fingerprint,
            preview_hash=record.preview_hash,
            profile_fingerprint=fingerprint,
        )

    def _read_report(self, row, payload, artifacts, check):
        if row["report"] is None:
            raise CsvIntakeConflict("csv_profile_not_ready")
        try:
            result = CsvProfileResult.model_validate_json(
                self.store.open(row["report"], b"csv-report-v1:" + self.store.aad(row))
            )
            expected = payload.file.model_copy(update={"status": "profiled"})
            if (
                result.file != expected
                or result.profile.row_count != payload.preview.row_count
                or tuple(c.name for c in result.profile.columns) != payload.preview.columns
                or result.profile.connector_id != "csv_upload"
                or result.profile.relation != "uploaded_csv"
                or any(
                    c.data_type != "TEXT"
                    or c.null_count != 0
                    or c.mean is not None
                    or c.row_count != payload.preview.row_count
                    for c in result.profile.columns
                )
            ):
                raise ValueError("binding")
            bodies = {}
            for ref in result.evidence:
                check()
                if (
                    ref.task_id != payload.file.task_id
                    or ref.artifact_type in bodies
                    or ref.uri != f"artifact://local/{ref.task_id}/{ref.artifact_id}.json"
                ):
                    raise ValueError("artifact_binding")
                content = artifacts.export_bytes(ref)
                if len(content) != ref.size_bytes:
                    raise ValueError("artifact_size")
                bodies[ref.artifact_type] = json.loads(content)
            if bodies.get("data_profile") != result.profile.model_dump(mode="json") or bodies.get(
                "csv_profile_provenance"
            ) != self._provenance(payload, result.profile_fingerprint).model_dump(mode="json"):
                raise ValueError("artifact_content")
            check()
            return result
        except PermissionError:
            raise
        except Exception as exc:
            raise CsvIntakeConflict("csv_profile_unavailable") from exc

    def result(self, file_id: UUID, actor: ActorContext, token: str):
        check = self.guard(actor, token)
        with self.store.db() as db:
            row, payload = self.store.owned(db, file_id, actor)
            bound = self._bound_guard(payload, check)
            self._parsed(payload, bound)
            return self._read_report(row, payload, self._artifacts(payload.actor), bound)

    def confirm(
        self,
        file_id: UUID,
        actor: ActorContext,
        token: str,
        request: ConfirmCsvText,
        *,
        deadline=None,
    ):
        request = ConfirmCsvText.model_validate(request.model_dump())
        end = self.monotonic() + self.limits.max_seconds
        if deadline is not None:
            end = min(end, deadline)
        check = self.guard(actor, token, deadline=end)
        artifacts = None
        try:
            with self.store.db(write=True) as db:
                row, payload = self.store.owned(db, file_id, actor)
                bound = self._bound_guard(payload, check)
                if (
                    request.source_hash != payload.file.source_hash
                    or request.preview_hash != payload.file.preview_hash
                ):
                    raise CsvIntakeConflict("csv_confirmation_source_or_preview_changed")
                parsed = self._parsed(payload, bound)
                artifacts = self._artifacts(payload.actor)
                if row["report"] is not None:
                    return self._read_report(row, payload, artifacts, bound)
                self.budgets.register(
                    payload.file.task_id,
                    payload.actor,
                    Budget(max_runtime_seconds=self.limits.max_seconds, max_tool_calls=2),
                )

                def running():
                    bound()
                    self.budgets.ensure_active(payload.file.task_id, payload.actor)

                running()
                self._audit("csv.profile_intent", payload)
                result = self._profile(payload, parsed, artifacts, running, end)
                running()
                encrypted = self.store.seal(
                    result.model_dump_json().encode(), b"csv-report-v1:" + self.store.aad(row)
                )
                result = self._read_report(
                    dict(row) | {"report": encrypted}, payload, artifacts, running
                )
                db.execute(
                    "UPDATE csv_uploads SET report=? WHERE file_id=?", (encrypted, str(file_id))
                )
                running()
                return result
        except Exception:
            if artifacts is not None:
                for ref in reversed(artifacts.created):
                    with suppress(Exception):
                        artifacts.discard(ref)
            raise

    def _profile(self, payload, parsed, artifacts, check, deadline):
        with TemporaryDirectory(prefix="csv-profile-", dir=self.scratch_root) as directory:
            path = Path(directory) / "snapshot.db"
            with closing(sqlite3.connect(path)) as db, db:
                columns = ",".join(
                    quote_identifier(c) + " TEXT NOT NULL" for c in parsed.preview.columns
                )
                db.execute(f'CREATE TABLE "uploaded_csv" ({columns})')
                placeholders = ",".join("?" for _ in parsed.preview.columns)
                db.executemany(f'INSERT INTO "uploaded_csv" VALUES ({placeholders})', parsed.rows)
            check()
            cancel = Event()
            remaining = max(0.001, deadline - self.monotonic())
            timer = Timer(remaining, cancel.set)
            timer.daemon = True
            timer.start()
            try:
                runtime = DataEngineeringRuntime(
                    connectors={
                        "csv_upload": SQLiteConnector(
                            path, connector_id="csv_upload", timeout_seconds=remaining
                        )
                    },
                    connector_domains={"csv_upload": frozenset(payload.actor.data_domains)},
                    artifacts=artifacts,
                    audit=self.audit,
                )
                runtime.gateway.budgets = self.budgets
                profile = runtime.profile_relation(
                    task_id=payload.file.task_id,
                    actor=payload.actor,
                    connector_id="csv_upload",
                    relation="uploaded_csv",
                    trace_id=str(payload.file.task_id),
                    cancel_event=cancel,
                )
                check()
                fingerprint = _PROFILE_FINGERPRINT
                artifacts.put_json(
                    task_id=payload.file.task_id,
                    artifact_type="csv_profile_provenance",
                    value=self._provenance(payload, fingerprint).model_dump(mode="json"),
                    created_by="data-engineer",
                )
                return CsvProfileResult(
                    file=payload.file.model_copy(update={"status": "profiled"}),
                    profile=profile,
                    profile_fingerprint=fingerprint,
                    evidence=tuple(artifacts.created),
                )
            finally:
                cancel.set()
                timer.cancel()
