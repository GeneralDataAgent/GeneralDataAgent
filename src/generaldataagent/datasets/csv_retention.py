"""Owner-scoped, read-only retained-source inventory. No deletion approval or apply port."""

import hashlib
import hmac
from datetime import timedelta
from time import monotonic
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, model_validator

from generaldataagent.contracts import ActorContext, AuditEvent
from generaldataagent.contracts.models import ContractModel
from generaldataagent.datasets.csv_intake_contracts import CsvProfileResult
from generaldataagent.datasets.csv_intake_store import (
    CsvIntakeUnavailable,
    CsvUploadStore,
    source_bytes,
)
from generaldataagent.memory.display_preferences import canonical_hash
from generaldataagent.orchestration.task_queue import canonical_actor
from generaldataagent.security.identity import SessionBroker


class CsvRetentionConflict(ValueError):
    pass


class CsvRetainedSource(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    file_id: UUID
    task_id: UUID
    raw_bytes: int = Field(ge=1, le=1048576, strict=True)
    created_at: AwareDatetime
    expires_at: AwareDatetime
    expired: bool = Field(strict=True)
    report_state: Literal["none_registered", "registered_not_verified"]
    report_artifact_ids: tuple[UUID, ...] = Field(max_length=2)
    hold_state: Literal["unknown"] = "unknown"
    decision: Literal["retain_active", "blocked_hold_unknown"]

    @model_validator(mode="after")
    def coherent(self):
        if (
            self.expires_at <= self.created_at
            or self.decision != ("blocked_hold_unknown" if self.expired else "retain_active")
            or len(self.report_artifact_ids)
            != (2 if self.report_state == "registered_not_verified" else 0)
            or len(set(self.report_artifact_ids)) != len(self.report_artifact_ids)
        ):
            raise ValueError("csv_retention_entry_invalid")
        return self


class CsvRetentionPlan(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["inventory_only"] = "inventory_only"
    scope_ref: str = Field(pattern=r"^[0-9a-f]{64}$")
    store_ref: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: AwareDatetime
    expires_at: AwareDatetime
    entries: tuple[CsvRetainedSource, ...] = Field(max_length=20)
    retained_files: int = Field(ge=0, le=20, strict=True)
    expired_files: int = Field(ge=0, le=20, strict=True)
    retained_raw_bytes: int = Field(ge=0, le=20 * 1048576, strict=True)
    snapshot_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    deletion_authorized: Literal[False] = False
    quota_released: Literal[0] = 0
    plan_token: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def coherent(self):
        if (
            self.retained_files != len(self.entries)
            or self.expired_files != sum(e.expired for e in self.entries)
            or self.retained_raw_bytes != sum(e.raw_bytes for e in self.entries)
            or self.expires_at != self.observed_at + timedelta(seconds=300)
            or any(e.expired != (e.expires_at <= self.observed_at) for e in self.entries)
            or tuple(sorted(e.file_id.int for e in self.entries))
            != tuple(e.file_id.int for e in self.entries)
            or len({e.file_id for e in self.entries}) != len(self.entries)
        ):
            raise ValueError("csv_retention_plan_invalid")
        return self


class CsvRetentionVerification(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    snapshot_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    verified_at: AwareDatetime
    retained_files: int = Field(ge=0, le=20, strict=True)
    deletion_authorized: Literal[False] = False
    quota_released: Literal[0] = 0


class CsvRetentionInspector:
    """Explicit local operator reader. Unknown hold state always blocks deletion eligibility.

    A signed snapshot comparison is neither an approval nor an audit/production qualification.
    SQLite mode=ro observes live WAL state; immutable=1 would hide concurrent committed changes.
    """

    def __init__(self, store: CsvUploadStore, broker: SessionBroker, *, audit, clock=monotonic):
        if not store.read_only:
            raise ValueError("csv_retention_requires_read_only_store")
        if not callable(getattr(audit, "emit", None)):
            raise ValueError("csv_retention_requires_audit")
        self.store, self.broker, self.audit, self.clock = store, broker, audit, clock

    def _guard(self, actor, token):
        if not self.store.read_only:
            raise ValueError("csv_retention_requires_read_only_store")
        actor = canonical_actor(actor)
        reference = self.broker.session_reference(token)
        deadline = self.clock() + 5

        def guard():
            identity = self.broker.authenticate_reference(reference)
            if (
                self.clock() >= deadline
                or identity.business_constraint is not None
                or "analysis:run" not in identity.permissions
                or (identity.user_id, identity.tenant_id, identity.project_id)
                != (actor.user_id, actor.tenant_id, actor.project_id)
                or not set(actor.data_domains) <= identity.data_domains
            ):
                raise PermissionError("csv_retention_unavailable")

        guard()
        return guard, deadline

    def _store_ref(self):
        return hmac.digest(
            self.store.scope_key, str(self.store.path.resolve()).encode(), "sha256"
        ).hex()

    def _token(self, fields):
        return hmac.digest(
            self.store.scope_key,
            ("csv-retention-plan-v1:" + canonical_hash(fields)).encode(),
            "sha256",
        ).hex()

    def _snapshot(self, actor, now, guard, deadline):
        entries, bindings = [], []
        scope = self.store.scope(actor)
        try:
            with self.store.db() as db:
                db.set_progress_handler(lambda: int(self.clock() >= deadline), 1000)
                rows = db.execute(
                    "SELECT * FROM csv_uploads WHERE scope=? ORDER BY file_id LIMIT 21", (scope,)
                )
                for row in rows:
                    guard()
                    if len(entries) >= 20:
                        raise CsvIntakeUnavailable("csv_retention_unavailable")
                    payload = self.store.decode(row)
                    if payload.actor != canonical_actor(actor):
                        raise PermissionError("csv_retention_unavailable")
                    refs = ()
                    if row["report"] is not None:
                        result = CsvProfileResult.model_validate_json(
                            self.store.open(row["report"], b"csv-report-v1:" + self.store.aad(row))
                        )
                        if (
                            result.file != payload.file.model_copy(update={"status": "profiled"})
                            or {ref.artifact_type for ref in result.evidence}
                            != {"data_profile", "csv_profile_provenance"}
                            or any(
                                ref.uri != f"artifact://local/{ref.task_id}/{ref.artifact_id}.json"
                                for ref in result.evidence
                            )
                        ):
                            raise ValueError("report_binding")
                        refs = tuple(
                            sorted(
                                (ref.artifact_id for ref in result.evidence), key=lambda v: v.int
                            )
                        )
                    expired = payload.file.expires_at <= now
                    entries.append(
                        CsvRetainedSource(
                            file_id=payload.file.file_id,
                            task_id=payload.file.task_id,
                            raw_bytes=len(source_bytes(payload)),
                            created_at=payload.file.created_at,
                            expires_at=payload.file.expires_at,
                            expired=expired,
                            report_state="registered_not_verified" if refs else "none_registered",
                            report_artifact_ids=refs,
                            decision="blocked_hold_unknown" if expired else "retain_active",
                        )
                    )
                    bindings.append(
                        {
                            "aad": self.store.aad(row).decode(),
                            "body": hashlib.sha256(row["body"]).hexdigest(),
                            "report": hashlib.sha256(row["report"]).hexdigest()
                            if row["report"] is not None
                            else None,
                        }
                    )
                    guard()
            guard()
        except (PermissionError, CsvIntakeUnavailable):
            raise
        except Exception as exc:
            raise CsvIntakeUnavailable("csv_retention_unavailable") from exc
        return tuple(entries), canonical_hash(
            {"scope": scope, "store": self._store_ref(), "rows": bindings}
        )

    def _audit(self, kind, actor, digest, count):
        try:
            self.audit.emit(
                AuditEvent(
                    event_type=kind,
                    task_id=uuid4(),
                    actor_id=actor.user_id,
                    tenant_id=actor.tenant_id,
                    trace_id=str(uuid4()),
                    occurred_at=self.store.now(),
                    payload={"retained_files": count, "snapshot_digest": digest},
                )
            )
        except Exception as exc:
            raise CsvIntakeUnavailable("csv_audit_unavailable") from exc

    def plan(self, actor: ActorContext, token: str) -> CsvRetentionPlan:
        guard, deadline = self._guard(actor, token)
        now = self.store.now()
        entries, digest = self._snapshot(actor, now, guard, deadline)
        fields = dict(
            scope_ref=self.store.scope(actor),
            store_ref=self._store_ref(),
            observed_at=now,
            expires_at=now + timedelta(seconds=300),
            entries=entries,
            retained_files=len(entries),
            expired_files=sum(e.expired for e in entries),
            retained_raw_bytes=sum(e.raw_bytes for e in entries),
            snapshot_digest=digest,
        )
        # Hash canonical JSON representations, including the fixed non-approval fields.
        provisional = CsvRetentionPlan(**fields, plan_token="0" * 64)
        result = provisional.model_copy(
            update={
                "plan_token": self._token(
                    provisional.model_dump(mode="json", exclude={"plan_token"})
                )
            }
        )
        guard()
        self._audit("csv.retention_inventory_planned", actor, digest, len(entries))
        guard()
        if not result.observed_at <= self.store.now() < result.expires_at:
            raise CsvRetentionConflict("csv_retention_plan_invalid")
        return result

    def verify(
        self, actor: ActorContext, token: str, plan: CsvRetentionPlan
    ) -> CsvRetentionVerification:
        guard, deadline = self._guard(actor, token)
        now = self.store.now()
        if (
            plan.scope_ref != self.store.scope(actor)
            or plan.store_ref != self._store_ref()
            or not plan.observed_at <= now < plan.expires_at
            or not hmac.compare_digest(
                plan.plan_token, self._token(plan.model_dump(mode="json", exclude={"plan_token"}))
            )
        ):
            raise CsvRetentionConflict("csv_retention_plan_invalid")
        entries, digest = self._snapshot(actor, now, guard, deadline)
        if digest != plan.snapshot_digest or entries != plan.entries:
            raise CsvRetentionConflict("csv_retention_plan_changed")
        guard()
        self._audit("csv.retention_snapshot_compared", actor, digest, len(entries))
        guard()
        if not plan.observed_at <= self.store.now() < plan.expires_at:
            raise CsvRetentionConflict("csv_retention_plan_invalid")
        return CsvRetentionVerification(
            snapshot_digest=digest, verified_at=now, retained_files=len(entries)
        )
