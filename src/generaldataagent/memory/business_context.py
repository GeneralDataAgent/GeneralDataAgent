"""Encrypted, private, CAS-versioned business definitions and project context.

Only an explicit user confirmation creates active memory; extracted candidates, model output and
free-floating notes never become active here. This module stores background that can be cited —
it cannot grant permissions, change tool policy, bypass approval or override a certified metric.
"""

import hashlib
import hmac
import json
import os
import sqlite3
import unicodedata
from collections.abc import Callable, Iterable
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import AwareDatetime, Field, field_validator, model_validator

from generaldataagent.contracts import ActorContext, AuditEvent
from generaldataagent.contracts.models import ContractModel
from generaldataagent.orchestration.task_queue import canonical_actor
from generaldataagent.security.dlp import TextDLPScanner


def canonical_hash(value: dict) -> str:
    raw = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


class BusinessContextConflict(ValueError):
    pass


class BusinessContextUnavailable(RuntimeError):
    pass


def _normalized_term(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _single_line(value: str) -> str:
    if value != value.strip() or any(unicodedata.category(ch) == "Cc" for ch in value):
        raise ValueError("business_context_term_invalid")
    return value


class BusinessContextEntry(ContractModel):
    """One confirmed term definition or project background note."""

    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["business_definition", "project_context"] = "business_definition"
    term: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=2_000)

    @field_validator("term")
    @classmethod
    def term_is_single_line(cls, value: str) -> str:
        return _single_line(value)

    @field_validator("text")
    @classmethod
    def text_is_bounded_memory(cls, value: str) -> str:
        if value != value.strip() or any(
            unicodedata.category(ch) == "Cc" and ch not in "\n\t" for ch in value
        ):
            raise ValueError("business_context_text_invalid")
        return value


class BusinessContext(ContractModel):
    """One confirmed snapshot of background the person chose to remember."""

    schema_version: Literal["1.0"] = "1.0"
    entries: tuple[BusinessContextEntry, ...] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def entries_are_unique(self) -> "BusinessContext":
        keys = [(entry.kind, _normalized_term(entry.term)) for entry in self.entries]
        if len(set(keys)) != len(keys):
            raise ValueError("business_context_duplicate_term_requires_clarification")
        return self

    def canonical(self) -> "BusinessContext":
        return BusinessContext(
            entries=tuple(sorted(self.entries, key=lambda item: (item.kind, item.term)))
        )


class ConfirmBusinessContext(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    expected_version: int = Field(ge=0, le=2**31 - 1, strict=True)
    confirmed: bool = Field(strict=True)
    value: BusinessContext
    valid_for_seconds: int = Field(default=30 * 86400, ge=1, le=365 * 86400, strict=True)

    @model_validator(mode="after")
    def explicit_confirmation(self):
        if not self.confirmed:
            raise ValueError("explicit_business_context_confirmation_required")
        return self


class BusinessContextRecord(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["business_context"] = "business_context"
    actor: ActorContext
    version: int = Field(ge=1, le=2**31 - 1, strict=True)
    status: Literal["confirmed", "revoked"]
    value: BusinessContext | None
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
            raise ValueError("business_context_binding_invalid")
        content = self.value.model_dump(mode="json") if self.value is not None else {}
        if self.content_hash != canonical_hash(content) or self.record_hash != canonical_hash(
            self.model_dump(mode="json", exclude={"record_hash"})
        ):
            raise ValueError("business_context_hash_invalid")
        return self


class BusinessContextState(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    version: int = Field(ge=0, le=2**31 - 1, strict=True)
    status: Literal["unset", "active", "expired", "revoked"]
    record: BusinessContextRecord | None = None

    @model_validator(mode="after")
    def state_binding(self):
        if (self.version == 0) != (self.status == "unset"):
            raise ValueError("business_context_state_binding_invalid")
        if self.status == "active":
            if (
                self.record is None
                or self.record.status != "confirmed"
                or self.record.version != self.version
            ):
                raise ValueError("business_context_state_binding_invalid")
        elif self.record is not None:
            raise ValueError("inactive_business_context_cannot_be_injected")
        return self


class ConfirmedBusinessContextStore:
    """Dedicated local store. Keys and DB must stay outside agent/artifact workspaces.

    Encrypted history is for citation, NOT active retrieval after revocation or expiry.
    A certified metric term cannot be redefined here; the definition lives in the catalog.
    """

    def __init__(
        self,
        path: Path,
        *,
        encryption_key: bytes,
        audit,
        scanner: TextDLPScanner | None = None,
        clock=None,
        certified_terms: Callable[[], Iterable[str]] | None = None,
        max_versions: int = 1000,
    ):
        if not isinstance(encryption_key, bytes) or len(encryption_key) != 32:
            raise ValueError("business_context_requires_aes256_key")
        if not callable(getattr(audit, "emit", None)):
            raise ValueError("business_context_requires_audit")
        if type(max_versions) is not int or not 1 <= max_versions <= 10000:
            raise ValueError("invalid_business_context_version_limit")
        if certified_terms is not None and not callable(certified_terms):
            raise ValueError("invalid_certified_term_provider")
        self.path, self.audit, self.max_versions = path, audit, max_versions
        self.scanner, self.clock = scanner or TextDLPScanner(), clock or (lambda: datetime.now(UTC))
        self.certified_terms = certified_terms or (lambda: ())
        self.cipher = AESGCM(encryption_key)
        self.scope_key = hmac.digest(encryption_key, b"gda-business-context-scope-v1", "sha256")
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("unsupported_business_context_schema")
            if version == 0:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                    raise ValueError("dedicated_business_context_database_required")
                db.execute(
                    "CREATE TABLE context_key (singleton INTEGER PRIMARY KEY, body BLOB NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE business_context (scope TEXT NOT NULL, "
                    "version INTEGER NOT NULL, "
                    "body BLOB NOT NULL, PRIMARY KEY(scope, version))"
                )
                db.execute(
                    "INSERT INTO context_key VALUES (1, ?)",
                    (self._seal(b"business-context-v1", b"key-v1"),),
                )
                db.execute("PRAGMA user_version=1")
            try:
                row = db.execute("SELECT body FROM context_key WHERE singleton=1").fetchone()
                if row is None or self._open(row[0], b"key-v1") != b"business-context-v1":
                    raise ValueError("key")
            except Exception as exc:
                raise PermissionError("business_context_store_unavailable") from exc

    def _now(self):
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("business_context_clock_must_be_aware")
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
        return f"gda-business-context-v1:{scope}:{version}".encode()

    @contextmanager
    def _db(self, *, write=False):
        try:
            with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
                db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                if db.execute("PRAGMA user_version").fetchone()[0] != 1:
                    raise ValueError("unsupported_business_context_schema")
                yield db
        except sqlite3.Error as exc:
            raise BusinessContextUnavailable("business_context_store_unavailable") from exc

    def _decode(self, row, actor):
        if row is None:
            return None
        scope, version, body = row
        try:
            record = BusinessContextRecord.model_validate_json(
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
            raise PermissionError("business_context_record_unavailable") from exc

    def _latest(self, db, actor):
        row = db.execute(
            "SELECT scope, version, body FROM business_context "
            "WHERE scope=? ORDER BY version DESC LIMIT 1",
            (self._scope(actor),),
        ).fetchone()
        return self._decode(row, actor)

    def _state(self, record):
        if record is None:
            return BusinessContextState(version=0, status="unset")
        status = (
            "revoked"
            if record.status == "revoked"
            else "expired"
            if record.expires_at <= self._now()
            else "active"
        )
        return BusinessContextState(
            version=record.version, status=status, record=record if status == "active" else None
        )

    def current(self, actor: ActorContext) -> BusinessContextState:
        with self._db() as db:
            return self._state(self._latest(db, actor))

    def history(self, actor: ActorContext, version: int) -> BusinessContextRecord:
        if type(version) is not int or not 1 <= version <= 2**31 - 1:
            raise ValueError("invalid_business_context_version")
        with self._db() as db:
            row = db.execute(
                "SELECT scope, version, body FROM business_context WHERE scope=? AND version=?",
                (self._scope(actor), version),
            ).fetchone()
            record = self._decode(row, actor)
            if record is None:
                raise PermissionError("business_context_record_unavailable")
            return record

    def emit(self, record, event_type: str):
        try:
            self.audit.emit(
                AuditEvent(
                    event_type=event_type,
                    task_id=record.source_id,
                    actor_id=record.actor.user_id,
                    tenant_id=record.actor.tenant_id,
                    trace_id=str(record.source_id),
                    occurred_at=self._now(),
                    payload={"version": record.version, "record_hash": record.record_hash},
                )
            )
        except Exception as exc:
            raise BusinessContextUnavailable("business_context_audit_unavailable") from exc

    def _refuse_certified_metrics(self, value: BusinessContext):
        certified = {_normalized_term(term) for term in self.certified_terms()}
        if not certified:
            return
        for entry in value.entries:
            if entry.kind == "business_definition" and _normalized_term(entry.term) in certified:
                raise BusinessContextConflict("business_context_cannot_override_certified_metric")

    def _append(self, db, actor, latest, value, valid_for_seconds, guard):
        version = latest.version + 1 if latest is not None else 1
        now = self._now()
        content = value.canonical().model_dump(mode="json") if value is not None else {}
        self.scanner.check(json.dumps(content, ensure_ascii=False).encode())
        fields = dict(
            schema_version="1.0",
            kind="business_context",
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
        record = BusinessContextRecord.model_validate(
            fields | {"record_hash": canonical_hash(fields)}
        )
        scope = self._scope(actor)
        guard()
        self.emit(record, "business_context.change_intent")
        db.execute(
            "INSERT INTO business_context VALUES (?, ?, ?)",
            (
                scope,
                version,
                self._seal(record.model_dump_json().encode(), self._aad(scope, version)),
            ),
        )
        guard()  # Revocation/audit/DB failure must not silently commit active memory.
        return self._state(record)

    def confirm(
        self, actor: ActorContext, request: ConfirmBusinessContext, *, guard: Callable[[], None]
    ):
        request = ConfirmBusinessContext.model_validate(request.model_dump())
        if request.value.entries:
            self._refuse_certified_metrics(request.value)
        guard()
        with self._db(write=True) as db:
            latest = self._latest(db, actor)
            if request.expected_version != (latest.version if latest else 0):
                raise BusinessContextConflict("business_context_version_conflict")
            retained = db.execute(
                "SELECT COUNT(*) FROM business_context WHERE scope=?", (self._scope(actor),)
            ).fetchone()[0]
            if retained >= self.max_versions:
                raise BusinessContextConflict("business_context_history_capacity_exhausted")
            return self._append(db, actor, latest, request.value, request.valid_for_seconds, guard)

    def revoke(self, actor: ActorContext, expected_version: int, *, guard: Callable[[], None]):
        if type(expected_version) is not int or not 0 <= expected_version <= 2**31 - 1:
            raise ValueError("invalid_business_context_version")
        guard()
        with self._db(write=True) as db:
            latest = self._latest(db, actor)
            if expected_version != (latest.version if latest else 0):
                raise BusinessContextConflict("business_context_version_conflict")
            if latest is None or latest.status == "revoked":
                return self._state(latest)
            return self._append(db, actor, latest, None, 0, guard)
