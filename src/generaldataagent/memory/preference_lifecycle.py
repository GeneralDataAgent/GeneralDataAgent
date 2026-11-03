"""Reviewed, bounded row purges of inactive display history; latest revision survives."""

import base64
import hmac
import re
from collections.abc import Callable
from datetime import timedelta
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, model_validator

from generaldataagent.contracts import ActorContext, AuditEvent
from generaldataagent.contracts.models import ContractModel
from generaldataagent.memory.display_preferences import (
    ConfirmedDisplayPreferenceStore,
    PreferenceConflict,
    PreferenceUnavailable,
    canonical_hash,
)

_Version = Annotated[int, Field(ge=1, le=2**31 - 1, strict=True)]


class PreferencePurgeMetadata(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    plan_id: UUID
    head_version: int = Field(ge=0, le=2**31 - 1, strict=True)
    after_version: int = Field(ge=0, le=2**31 - 1, strict=True)
    next_after_version: int = Field(ge=0, le=2**31 - 1, strict=True)
    scan_limit: int = Field(ge=1, le=200, strict=True)
    scanned: int = Field(ge=0, le=200, strict=True)
    candidate_versions: tuple[_Version, ...] = Field(max_length=200)
    has_more: bool = Field(strict=True)
    retention_seconds: int = Field(ge=60, le=90 * 86400, strict=True)
    created_at: AwareDatetime
    cutoff: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def bounds(self):
        versions = self.candidate_versions
        if (
            not self.after_version <= self.next_after_version <= self.head_version
            or self.scanned > self.scan_limit
            or len(versions) > self.scanned
            or any(
                type(v) is not int
                or not self.after_version < v <= self.next_after_version < self.head_version
                for v in versions
            )
            or tuple(sorted(set(versions))) != versions
            or (self.scanned > 0 and self.next_after_version <= self.after_version)
            or (self.has_more and self.scanned != self.scan_limit)
            or (
                not self.scanned
                and (self.next_after_version != self.after_version or self.has_more)
            )
            or self.cutoff != self.created_at - timedelta(seconds=self.retention_seconds)
            or self.expires_at != self.created_at + timedelta(seconds=300)
        ):
            raise ValueError("preference_purge_plan_binding_invalid")
        return self


class PreferencePurgePlan(PreferencePurgeMetadata):
    plan_token: str = Field(min_length=64, max_length=65536, pattern=r"^[A-Za-z0-9_-]+$")


class PreferencePurgeResult(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    plan_id: UUID
    head_version: int = Field(ge=0)
    deleted: int = Field(ge=0, le=200)
    already_absent: int = Field(ge=0, le=200)
    candidate_versions: tuple[_Version, ...]

    @model_validator(mode="after")
    def counts_match_candidates(self):
        if (
            self.deleted + self.already_absent != len(self.candidate_versions)
            or tuple(sorted(set(self.candidate_versions))) != self.candidate_versions
            or any(v >= self.head_version for v in self.candidate_versions)
        ):
            raise ValueError("preference_purge_result_binding_invalid")
        return self


class _Candidate(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    version: int = Field(ge=1, le=2**31 - 1, strict=True)
    record_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class _Binding(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    scope: str = Field(pattern=r"^[0-9a-f]{64}$")
    store_ref: str = Field(pattern=r"^[0-9a-f]{64}$")
    metadata: PreferencePurgeMetadata
    head_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    candidates: tuple[_Candidate, ...] = Field(max_length=200)


class PreferenceHistoryMaintenance:
    """Trusted local port; callers must provide a live scope guard for every operation.

    This removes SQLite rows, not bytes from free pages/WAL/backups, cached snapshots
    or other stores. Retention applies to historical citations after inactivity. The
    latest record (including expired value or revocation tombstone) is always retained.
    """

    def __init__(self, store: ConfirmedDisplayPreferenceStore):
        self.store = store

    def _store_ref(self):
        return hmac.digest(
            self.store.scope_key, str(self.store.path.resolve()).encode(), "sha256"
        ).hex()

    def _aad(self, scope):
        return f"gda-display-preference-purge-v1:{scope}:{self._store_ref()}".encode()

    def plan(
        self,
        actor: ActorContext,
        *,
        guard: Callable[[], None],
        after_version: int = 0,
        scan_limit: int = 50,
        retention_seconds: int = 7 * 86400,
    ) -> PreferencePurgePlan:
        for value, lower, upper in (
            (after_version, 0, 2**31 - 1),
            (scan_limit, 1, 200),
            (retention_seconds, 60, 90 * 86400),
        ):
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError("invalid_preference_purge_limits")
        guard()
        store = self.store
        now, scope = store._now(), store._scope(actor)
        cutoff = now - timedelta(seconds=retention_seconds)
        with store._db() as db:
            head = store._latest(db, actor)
            head_version = head.version if head is not None else 0
            if after_version > head_version:
                raise PreferenceConflict("preference_purge_cursor_outside_history")
            rows = db.execute(
                "SELECT scope, version, body FROM display_preferences WHERE scope=? "
                "AND version>? AND version<? ORDER BY version LIMIT ?",
                (scope, after_version, head_version, scan_limit + 1),
            ).fetchall()
            records = [store._decode(row, actor) for row in rows]
            scanned = records[:scan_limit]
            candidates = []
            for index, record in enumerate(scanned):
                guard()
                successor = records[index + 1] if index + 1 < len(records) else head
                if (
                    successor is None
                    or successor.created_at < record.created_at
                    or head.created_at < successor.created_at
                ):
                    raise PreferenceUnavailable("preference_history_clock_order_unavailable")
                retired_at = successor.created_at
                if record.expires_at is not None:
                    retired_at = min(retired_at, record.expires_at)
                if retired_at <= cutoff:
                    candidates.append(
                        _Candidate(version=record.version, record_hash=record.record_hash)
                    )
            metadata = PreferencePurgeMetadata(
                plan_id=uuid4(),
                head_version=head_version,
                after_version=after_version,
                next_after_version=scanned[-1].version if scanned else after_version,
                scan_limit=scan_limit,
                scanned=len(scanned),
                has_more=len(records) > scan_limit,
                candidate_versions=tuple(c.version for c in candidates),
                retention_seconds=retention_seconds,
                created_at=now,
                cutoff=cutoff,
                expires_at=now + timedelta(seconds=300),
            )
            binding = _Binding(
                scope=scope,
                store_ref=self._store_ref(),
                metadata=metadata,
                head_hash=head.record_hash if head is not None else None,
                candidates=tuple(candidates),
            )
            token = (
                base64.urlsafe_b64encode(
                    store._seal(binding.model_dump_json().encode(), self._aad(scope))
                )
                .decode()
                .rstrip("=")
            )
            guard()
            self._check_time(metadata)
            return PreferencePurgePlan(**metadata.model_dump(), plan_token=token)

    def _binding(self, actor, plan):
        try:
            plan = PreferencePurgePlan.model_validate(plan.model_dump())
            token = plan.plan_token
            if not re.fullmatch(r"[A-Za-z0-9_-]{64,65536}", token):
                raise ValueError("encoding")
            scope = self.store._scope(actor)
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            binding = _Binding.model_validate_json(self.store._open(raw, self._aad(scope)))
            metadata = PreferencePurgeMetadata.model_validate(
                plan.model_dump(exclude={"plan_token"})
            )
            if (
                binding.scope != scope
                or binding.store_ref != self._store_ref()
                or binding.metadata != metadata
                or tuple(c.version for c in binding.candidates) != metadata.candidate_versions
            ):
                raise ValueError("review_binding")
            self._check_time(metadata)
            return binding
        except Exception as exc:
            raise PermissionError("preference_purge_plan_unavailable") from exc

    def _check_time(self, metadata):
        if not metadata.created_at <= self.store._now() < metadata.expires_at:
            raise PermissionError("preference_purge_plan_unavailable")

    def apply(
        self, actor: ActorContext, plan: PreferencePurgePlan, *, guard: Callable[[], None]
    ) -> PreferencePurgeResult:
        guard()
        binding = self._binding(actor, plan)
        metadata, store = binding.metadata, self.store
        with store._db(write=True) as db:
            head = store._latest(db, actor)
            if (head.version if head else 0, head.record_hash if head else None) != (
                metadata.head_version,
                binding.head_hash,
            ):
                raise PreferenceConflict("preference_purge_head_changed")
            # The sealed plan proves eligibility at its fixed cutoff. Hashes and an
            # unchanged head bind those immutable inputs even if another reviewed purge
            # has since removed a successor/candidate. Missing rows mean only absent,
            # never proof that this particular plan previously committed.
            present = []
            for candidate in binding.candidates:
                guard()
                row = db.execute(
                    "SELECT scope, version, body FROM display_preferences "
                    "WHERE scope=? AND version=?",
                    (binding.scope, candidate.version),
                ).fetchone()
                record = store._decode(row, actor)
                if record is not None:
                    if (
                        record.record_hash != candidate.record_hash
                        or record.version >= metadata.head_version
                    ):
                        raise PermissionError("preference_purge_record_changed")
                    present.append(candidate.version)
            guard()
            self._check_time(metadata)
            if binding.candidates:
                try:
                    store.audit.emit(
                        AuditEvent(
                            event_type="display_preference.purge_intent",
                            task_id=metadata.plan_id,
                            actor_id=actor.user_id,
                            tenant_id=actor.tenant_id,
                            trace_id=str(metadata.plan_id),
                            occurred_at=store._now(),
                            payload={
                                "head_version": metadata.head_version,
                                "head_record_hash": binding.head_hash,
                                "scope": binding.scope,
                                "store_ref": binding.store_ref,
                                "candidate_versions": metadata.candidate_versions,
                                "plan_hash": canonical_hash(metadata.model_dump(mode="json")),
                                "candidate_count": len(binding.candidates),
                                "present_count": len(present),
                            },
                        )
                    )
                except Exception as exc:
                    raise PreferenceUnavailable("display_preference_audit_unavailable") from exc
            db.executemany(
                "DELETE FROM display_preferences WHERE scope=? AND version=?",
                [(binding.scope, version) for version in present],
            )
            guard()
            self._check_time(metadata)
            return PreferencePurgeResult(
                plan_id=metadata.plan_id,
                head_version=metadata.head_version,
                candidate_versions=metadata.candidate_versions,
                deleted=len(present),
                already_absent=len(binding.candidates) - len(present),
            )
