"""Encrypted, private, CAS-versioned display preferences; no model-written memory."""

import hashlib
import hmac
import json
import os
import sqlite3
from collections.abc import Callable
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import AwareDatetime, Field, model_validator

from generaldataagent.contracts import ActorContext, AuditEvent
from generaldataagent.contracts.models import ContractModel
from generaldataagent.orchestration.task_queue import canonical_actor
from generaldataagent.security.dlp import TextDLPScanner


def canonical_hash(value: dict) -> str:
    raw = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


class PreferenceConflict(ValueError):
    pass


class PreferenceUnavailable(RuntimeError):
    pass


class MetricDisplayPreference(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    layout: Literal["summary", "table"] = "table"
    preview_rows: int = Field(default=20, ge=1, le=50, strict=True)


class ConfirmDisplayPreference(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    expected_version: int = Field(ge=0, le=2**31 - 1, strict=True)
    confirmed: bool = Field(strict=True)
    value: MetricDisplayPreference
    valid_for_seconds: int = Field(default=86400, ge=1, le=30 * 86400, strict=True)

    @model_validator(mode="after")
    def explicit_confirmation(self):
        if not self.confirmed:
            raise ValueError("explicit_preference_confirmation_required")
        return self


class DisplayPreferenceRecord(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["metric_display"] = "metric_display"
    actor: ActorContext
    version: int = Field(ge=1, le=2**31 - 1, strict=True)
    status: Literal["confirmed", "revoked"]
    value: MetricDisplayPreference | None
    source_id: UUID
    source: Literal["explicit_user_confirmation", "explicit_user_revocation"]
    created_at: AwareDatetime
    expires_at: AwareDatetime | None
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    record_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def bindings(self):
        confirmed = self.status == "confirmed"
        if (
            confirmed != (self.value is not None)
            or confirmed != (self.expires_at is not None)
            or confirmed != (self.source == "explicit_user_confirmation")
            or self.actor != canonical_actor(self.actor)
            or (self.expires_at is not None and self.expires_at <= self.created_at)
        ):
            raise ValueError("display_preference_binding_invalid")
        content = self.value.model_dump(mode="json") if self.value is not None else {}
        if self.content_hash != canonical_hash(content) or self.record_hash != canonical_hash(
            self.model_dump(mode="json", exclude={"record_hash"})
        ):
            raise ValueError("display_preference_hash_invalid")
        return self


class DisplayPreferenceState(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    version: int = Field(ge=0, le=2**31 - 1, strict=True)
    status: Literal["unset", "active", "expired", "revoked"]
    record: DisplayPreferenceRecord | None = None

    @model_validator(mode="after")
    def state_binding(self):
        if (self.version == 0) != (self.status == "unset"):
            raise ValueError("display_preference_state_binding_invalid")
        if self.status == "active":
            if (
                self.record is None
                or self.record.status != "confirmed"
                or self.record.version != self.version
            ):
                raise ValueError("display_preference_state_binding_invalid")
        elif self.record is not None:
            raise ValueError("inactive_preference_cannot_be_injected")
        return self


class ConfirmedDisplayPreferenceStore:
    """Dedicated local store. Keys and DB must stay outside agent/artifact workspaces.

    Append-only encrypted history is for citation, NOT active retrieval after revocation.
    A local audit intent precedes commit; neither cross-store atomicity nor signed audit
    is claimed. Full-history rollback and production/shared-store qualification are separate.
    """

    def __init__(
        self,
        path: Path,
        *,
        encryption_key: bytes,
        audit,
        scanner: TextDLPScanner | None = None,
        clock=None,
        max_versions: int = 1000,
    ):
        if not isinstance(encryption_key, bytes) or len(encryption_key) != 32:
            raise ValueError("display_preferences_require_aes256_key")
        if not callable(getattr(audit, "emit", None)):
            raise ValueError("display_preferences_require_audit")
        if type(max_versions) is not int or not 1 <= max_versions <= 10000:
            raise ValueError("invalid_preference_version_limit")
        self.path, self.audit, self.max_versions = path, audit, max_versions
        self.scanner, self.clock = scanner or TextDLPScanner(), clock or (lambda: datetime.now(UTC))
        self.cipher = AESGCM(encryption_key)
        self.scope_key = hmac.digest(encryption_key, b"gda-display-preference-scope-v1", "sha256")
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("unsupported_display_preference_schema")
            if version == 0:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                    raise ValueError("dedicated_preference_database_required")
                db.execute(
                    "CREATE TABLE preference_key "
                    "(singleton INTEGER PRIMARY KEY, body BLOB NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE display_preferences (scope TEXT NOT NULL, "
                    "version INTEGER NOT NULL, "
                    "body BLOB NOT NULL, PRIMARY KEY(scope, version))"
                )
                db.execute(
                    "INSERT INTO preference_key VALUES (1, ?)",
                    (self._seal(b"display-preferences-v1", b"key-v1"),),
                )
                db.execute("PRAGMA user_version=1")
            try:
                row = db.execute("SELECT body FROM preference_key WHERE singleton=1").fetchone()
                if row is None or self._open(row[0], b"key-v1") != b"display-preferences-v1":
                    raise ValueError("key")
            except Exception as exc:
                raise PermissionError("display_preference_store_unavailable") from exc

    def _now(self):
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("preference_clock_must_be_aware")
        return now

    def _scope(self, actor):
        return hmac.digest(
            self.scope_key, canonical_actor(actor).model_dump_json().encode(), "sha256"
        ).hex()

    def _seal(self, raw, aad):
        nonce = os.urandom(12)
        return nonce + self.cipher.encrypt(nonce, raw, aad)

    def _open(self, body, aad):
        return self.cipher.decrypt(body[:12], body[12:], aad)

    @staticmethod
    def _aad(scope, version):
        return f"gda-display-preference-v1:{scope}:{version}".encode()

    @contextmanager
    def _db(self, *, write=False):
        try:
            with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
                db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                if db.execute("PRAGMA user_version").fetchone()[0] != 1:
                    raise ValueError("unsupported_display_preference_schema")
                yield db
        except sqlite3.Error as exc:
            raise PreferenceUnavailable("display_preference_store_unavailable") from exc

    def _decode(self, row, actor):
        if row is None:
            return None
        scope, version, body = row
        try:
            record = DisplayPreferenceRecord.model_validate_json(
                self._open(body, self._aad(scope, version))
            )
            if (
                record.actor != canonical_actor(actor)
                or record.version != version
                or scope != self._scope(actor)
            ):
                raise ValueError("binding")
            return record
        except Exception as exc:
            raise PermissionError("display_preference_record_unavailable") from exc

    def _latest(self, db, actor):
        row = db.execute(
            "SELECT scope, version, body FROM display_preferences "
            "WHERE scope=? ORDER BY version DESC LIMIT 1",
            (self._scope(actor),),
        ).fetchone()
        return self._decode(row, actor)

    def _state(self, record):
        if record is None:
            return DisplayPreferenceState(version=0, status="unset")
        status = (
            "revoked"
            if record.status == "revoked"
            else "expired"
            if record.expires_at <= self._now()
            else "active"
        )
        return DisplayPreferenceState(
            version=record.version, status=status, record=record if status == "active" else None
        )

    def current(self, actor: ActorContext) -> DisplayPreferenceState:
        with self._db() as db:
            return self._state(self._latest(db, actor))

    def history(self, actor: ActorContext, version: int) -> DisplayPreferenceRecord:
        if type(version) is not int or not 1 <= version <= 2**31 - 1:
            raise ValueError("invalid_preference_version")
        with self._db() as db:
            row = db.execute(
                "SELECT scope, version, body FROM display_preferences WHERE scope=? AND version=?",
                (self._scope(actor), version),
            ).fetchone()
            record = self._decode(row, actor)
            if record is None:
                raise PermissionError("display_preference_record_unavailable")
            return record

    def emit(self, record, event_type: str, *, task_id=None):
        try:
            self.audit.emit(
                AuditEvent(
                    event_type=event_type,
                    task_id=task_id or record.source_id,
                    actor_id=record.actor.user_id,
                    tenant_id=record.actor.tenant_id,
                    trace_id=str(task_id or record.source_id),
                    occurred_at=self._now(),
                    payload={"version": record.version, "record_hash": record.record_hash},
                )
            )
        except Exception as exc:
            raise PreferenceUnavailable("display_preference_audit_unavailable") from exc

    def _append(self, db, actor, latest, value, valid_for_seconds, guard):
        version = latest.version + 1 if latest is not None else 1
        now = self._now()
        content = value.model_dump(mode="json") if value is not None else {}
        self.scanner.check(json.dumps(content).encode())
        fields = dict(
            schema_version="1.0",
            kind="metric_display",
            actor=canonical_actor(actor).model_dump(mode="json"),
            version=version,
            status="confirmed" if value is not None else "revoked",
            value=content if value is not None else None,
            source_id=str(uuid4()),
            source="explicit_user_confirmation"
            if value is not None
            else "explicit_user_revocation",
            created_at=now.isoformat().replace("+00:00", "Z"),
            expires_at=(now + timedelta(seconds=valid_for_seconds))
            .isoformat()
            .replace("+00:00", "Z")
            if value is not None
            else None,
            content_hash=canonical_hash(content),
        )
        record = DisplayPreferenceRecord.model_validate(
            fields | {"record_hash": canonical_hash(fields)}
        )
        scope = self._scope(actor)
        guard()
        self.emit(record, "display_preference.change_intent")
        db.execute(
            "INSERT INTO display_preferences VALUES (?, ?, ?)",
            (
                scope,
                version,
                self._seal(record.model_dump_json().encode(), self._aad(scope, version)),
            ),
        )
        guard()  # Revocation/audit/DB failure must not silently commit active memory.
        return self._state(record)

    def confirm(
        self, actor: ActorContext, request: ConfirmDisplayPreference, *, guard: Callable[[], None]
    ):
        request = ConfirmDisplayPreference.model_validate(request.model_dump())
        guard()
        with self._db(write=True) as db:
            latest = self._latest(db, actor)
            if request.expected_version != (latest.version if latest else 0):
                raise PreferenceConflict("display_preference_version_conflict")
            if latest is not None and latest.version >= self.max_versions:
                raise PreferenceConflict("display_preference_history_capacity_exhausted")
            return self._append(db, actor, latest, request.value, request.valid_for_seconds, guard)

    def revoke(self, actor: ActorContext, expected_version: int, *, guard: Callable[[], None]):
        if type(expected_version) is not int or not 0 <= expected_version <= 2**31 - 1:
            raise ValueError("invalid_preference_version")
        guard()
        with self._db(write=True) as db:
            latest = self._latest(db, actor)
            if expected_version != (latest.version if latest else 0):
                raise PreferenceConflict("display_preference_version_conflict")
            if latest is None or latest.status == "revoked":
                return self._state(latest)
            return self._append(db, actor, latest, None, 0, guard)
