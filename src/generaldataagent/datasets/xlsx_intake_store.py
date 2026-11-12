"""Dedicated encrypted creator-private XLSX staging; preview only, no report or profile."""

import hashlib
import hmac
import json
import os
import re
import sqlite3
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from generaldataagent.datasets.xlsx_intake_contracts import _XlsxPayload
from generaldataagent.memory.display_preferences import canonical_hash
from generaldataagent.orchestration.task_queue import canonical_actor


class XlsxIntakeConflict(ValueError):
    pass


class XlsxIntakeUnavailable(RuntimeError):
    pass


def preview_hash(source_hash, parser_fingerprint, processing_fingerprint, preflight, preview):
    return canonical_hash(
        {
            "source_hash": source_hash,
            "parser_fingerprint": parser_fingerprint,
            "processing_fingerprint": processing_fingerprint,
            "preflight": preflight.model_dump(mode="json"),
            "preview": preview.model_dump(mode="json"),
        }
    )


class XlsxUploadStore:
    def __init__(
        self,
        path: Path,
        *,
        encryption_key: bytes,
        clock=None,
        max_files_per_actor: int = 20,
        max_total_files: int = 200,
    ):
        if not isinstance(encryption_key, bytes) or len(encryption_key) != 32:
            raise ValueError("xlsx_intake_requires_aes256_key")
        for value, upper in ((max_files_per_actor, 20), (max_total_files, 200)):
            if type(value) is not int or not 1 <= value <= upper:
                raise ValueError("invalid_xlsx_retained_capacity")
        self.path, self.clock = path, clock or (lambda: datetime.now(UTC))
        self.max_files_per_actor, self.max_total_files = max_files_per_actor, max_total_files
        self.cipher = AESGCM(encryption_key)
        self.scope_key = hmac.digest(encryption_key, b"gda-xlsx-intake-scope-v1", "sha256")
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("unsupported_xlsx_intake_schema")
            if version == 0:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                    raise ValueError("dedicated_xlsx_database_required")
                db.execute(
                    "CREATE TABLE xlsx_key (singleton INTEGER PRIMARY KEY, body BLOB NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE xlsx_uploads (file_id TEXT PRIMARY KEY, scope TEXT NOT NULL, "
                    "idem TEXT NOT NULL, request_hash TEXT NOT NULL, body BLOB NOT NULL, "
                    "source BLOB NOT NULL, UNIQUE(scope,idem))"
                )
                db.execute(
                    "INSERT INTO xlsx_key VALUES (1,?)",
                    (self.seal(b"xlsx-intake-v1", b"xlsx-key-v1"),),
                )
                db.execute("PRAGMA user_version=1")
            try:
                key = db.execute("SELECT body FROM xlsx_key WHERE singleton=1").fetchone()
                columns = [r[1] for r in db.execute("PRAGMA table_info(xlsx_uploads)")]
                if (
                    key is None
                    or self.open(key[0], b"xlsx-key-v1") != b"xlsx-intake-v1"
                    or columns != ["file_id", "scope", "idem", "request_hash", "body", "source"]
                ):
                    raise ValueError("layout_or_key")
            except Exception as exc:
                raise PermissionError("xlsx_intake_store_unavailable") from exc

    def now(self):
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("xlsx_intake_clock_must_be_aware")
        return now

    def scope(self, actor):
        return hmac.digest(
            self.scope_key, canonical_actor(actor).model_dump_json().encode(), "sha256"
        ).hex()

    def seal(self, content, aad):
        nonce = os.urandom(12)
        return nonce + self.cipher.encrypt(nonce, content, aad)

    def open(self, body, aad):
        return self.cipher.decrypt(body[:12], body[12:], aad)

    @staticmethod
    def aad(row):
        return json.dumps(
            [row["file_id"], row["scope"], row["idem"], row["request_hash"]], separators=(",", ":")
        ).encode()

    def body_aad(self, row):
        return b"xlsx-body-v1:" + self.aad(row)

    def source_aad(self, row):
        return b"xlsx-source-v1:" + self.aad(row)

    def request_hash(self, payload):
        content = canonical_hash(
            {
                "source_hash": payload.file.source_hash,
                "parser_fingerprint": payload.file.parser_fingerprint,
                "processing_fingerprint": payload.file.processing_fingerprint,
                "config_hash": payload.file.config_hash,
                "preflight_digest": payload.file.preflight_digest,
            }
        )
        return hmac.digest(self.scope_key, content.encode(), "sha256").hex()

    @contextmanager
    def db(self, *, write=False):
        try:
            with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
                db.row_factory = sqlite3.Row
                db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                if db.execute("PRAGMA user_version").fetchone()[0] != 1:
                    raise XlsxIntakeUnavailable("unsupported_xlsx_intake_schema")
                yield db
        except sqlite3.Error as exc:
            raise XlsxIntakeUnavailable("xlsx_intake_store_unavailable") from exc

    def decode(self, row):
        try:
            payload = _XlsxPayload.model_validate_json(self.open(row["body"], self.body_aad(row)))
            raw = self.open(row["source"], self.source_aad(row))
            if (
                payload.file.status != "staged"
                or str(payload.file.file_id) != row["file_id"]
                or payload.actor != canonical_actor(payload.actor)
                or self.scope(payload.actor) != row["scope"]
                or self.request_hash(payload) != row["request_hash"]
                or payload.file.source_hash != "sha256:" + hashlib.sha256(raw).hexdigest()
                or payload.file.byte_count != len(raw)
                or payload.file.config_hash
                != canonical_hash(payload.file.config.model_dump(mode="json"))
                or payload.file.preflight_digest
                != canonical_hash(payload.preflight.model_dump(mode="json"))
                or payload.file.preview_hash
                != preview_hash(
                    payload.file.source_hash,
                    payload.file.parser_fingerprint,
                    payload.file.processing_fingerprint,
                    payload.preflight,
                    payload.preview,
                )
                or payload.preflight.content_sha256 != payload.file.source_hash
                or payload.preview.sheet != payload.file.config.sheet
                or payload.file.expires_at <= payload.file.created_at
            ):
                raise ValueError("binding")
            return payload, raw
        except Exception as exc:
            raise PermissionError("xlsx_file_unavailable") from exc

    def owned(self, db, file_id, actor):
        row = db.execute("SELECT * FROM xlsx_uploads WHERE file_id=?", (str(file_id),)).fetchone()
        if row is None or row["scope"] != self.scope(actor):
            raise PermissionError("xlsx_file_unavailable")
        payload, raw = self.decode(row)
        if payload.actor != canonical_actor(actor) or payload.file.expires_at <= self.now():
            raise PermissionError("xlsx_file_unavailable")
        return row, payload, raw

    def stage(self, payload, raw, idempotency_key, guard, audit):
        if not isinstance(idempotency_key, str) or not re.fullmatch(
            r"[A-Za-z0-9._:-]{1,128}", idempotency_key
        ):
            raise XlsxIntakeConflict("xlsx_idempotency_key_invalid")
        if type(raw) is not bytes or len(raw) != payload.file.byte_count:
            raise XlsxIntakeConflict("xlsx_source_binding_invalid")
        scope = self.scope(payload.actor)
        idem = hmac.digest(
            self.scope_key, b"xlsx-idem-v1:" + idempotency_key.encode(), "sha256"
        ).hex()
        request_hash = self.request_hash(payload)
        guard()
        with self.db(write=True) as db:
            row = db.execute(
                "SELECT * FROM xlsx_uploads WHERE scope=? AND idem=?", (scope, idem)
            ).fetchone()
            if row is not None:
                old, _ = self.decode(row)
                if old.file.expires_at <= self.now():
                    raise PermissionError("xlsx_file_unavailable")
                if row["request_hash"] != request_hash:
                    raise XlsxIntakeConflict("xlsx_idempotency_request_changed")
                guard()
                return dict(row), old
            retained = db.execute(
                "SELECT COUNT(*) FROM xlsx_uploads WHERE scope=?", (scope,)
            ).fetchone()[0]
            total = db.execute("SELECT COUNT(*) FROM xlsx_uploads").fetchone()[0]
            if retained >= self.max_files_per_actor or total >= self.max_total_files:
                raise XlsxIntakeConflict("xlsx_retained_capacity_exhausted")
            binding = {
                "file_id": str(payload.file.file_id),
                "scope": scope,
                "idem": idem,
                "request_hash": request_hash,
            }
            audit()
            db.execute(
                "INSERT INTO xlsx_uploads (file_id,scope,idem,request_hash,body,source) "
                "VALUES (?,?,?,?,?,?)",
                (
                    *binding.values(),
                    self.seal(payload.model_dump_json().encode(), self.body_aad(binding)),
                    self.seal(raw, self.source_aad(binding)),
                ),
            )
            guard()
            return binding, payload
