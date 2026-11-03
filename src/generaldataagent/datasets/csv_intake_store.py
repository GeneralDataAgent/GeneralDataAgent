"""Dedicated encrypted creator-private CSV staging and atomic report registration."""

import base64
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

from generaldataagent.datasets.csv_intake_contracts import _CsvPayload
from generaldataagent.memory.display_preferences import canonical_hash
from generaldataagent.orchestration.task_queue import canonical_actor


class CsvIntakeConflict(ValueError):
    pass


class CsvIntakeUnavailable(RuntimeError):
    pass


def source_bytes(payload: _CsvPayload) -> bytes:
    return base64.b64decode(payload.source_base64, validate=True)


def preview_hash(source_hash, fingerprint, preview, processing_fingerprint):
    return canonical_hash(
        {
            "source_hash": source_hash,
            "parser_fingerprint": fingerprint,
            "processing_fingerprint": processing_fingerprint,
            "preview": preview.model_dump(mode="json"),
        }
    )


class CsvUploadStore:
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
            raise ValueError("csv_intake_requires_aes256_key")
        for value, upper in ((max_files_per_actor, 20), (max_total_files, 200)):
            if type(value) is not int or not 1 <= value <= upper:
                raise ValueError("invalid_csv_retained_capacity")
        self.path, self.clock = path, clock or (lambda: datetime.now(UTC))
        self.max_files_per_actor, self.max_total_files = max_files_per_actor, max_total_files
        self.cipher = AESGCM(encryption_key)
        self.scope_key = hmac.digest(encryption_key, b"gda-csv-intake-scope-v1", "sha256")
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("unsupported_csv_intake_schema")
            if version == 0:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                    raise ValueError("dedicated_csv_database_required")
                db.execute(
                    "CREATE TABLE csv_key (singleton INTEGER PRIMARY KEY, body BLOB NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE csv_uploads (file_id TEXT PRIMARY KEY, scope TEXT NOT NULL, "
                    "idem TEXT NOT NULL, request_hash TEXT NOT NULL, body BLOB NOT NULL, "
                    "report BLOB, UNIQUE(scope,idem))"
                )
                db.execute(
                    "INSERT INTO csv_key VALUES (1,?)",
                    (self.seal(b"csv-intake-v1", b"csv-key-v1"),),
                )
                db.execute("PRAGMA user_version=1")
            try:
                key = db.execute("SELECT body FROM csv_key WHERE singleton=1").fetchone()
                columns = [r[1] for r in db.execute("PRAGMA table_info(csv_uploads)")]
                if (
                    key is None
                    or self.open(key[0], b"csv-key-v1") != b"csv-intake-v1"
                    or columns != ["file_id", "scope", "idem", "request_hash", "body", "report"]
                ):
                    raise ValueError("layout_or_key")
            except Exception as exc:
                raise PermissionError("csv_intake_store_unavailable") from exc

    def now(self):
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("csv_intake_clock_must_be_aware")
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

    def request_hash(self, payload):
        content = canonical_hash(
            {
                "source_hash": payload.file.source_hash,
                "parser_fingerprint": payload.file.parser_fingerprint,
                "processing_fingerprint": payload.file.processing_fingerprint,
                "config_hash": payload.file.config_hash,
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
                    raise CsvIntakeUnavailable("unsupported_csv_intake_schema")
                yield db
        except sqlite3.Error as exc:
            raise CsvIntakeUnavailable("csv_intake_store_unavailable") from exc

    def decode(self, row):
        try:
            payload = _CsvPayload.model_validate_json(self.open(row["body"], self.aad(row)))
            raw = source_bytes(payload)
            if (
                payload.file.status != "staged"
                or str(payload.file.file_id) != row["file_id"]
                or payload.actor != canonical_actor(payload.actor)
                or self.scope(payload.actor) != row["scope"]
                or self.request_hash(payload) != row["request_hash"]
                or payload.file.source_hash != "sha256:" + hashlib.sha256(raw).hexdigest()
                or payload.file.config_hash
                != canonical_hash(payload.file.config.model_dump(mode="json"))
                or payload.file.preview_hash
                != preview_hash(
                    payload.file.source_hash,
                    payload.file.parser_fingerprint,
                    payload.preview,
                    payload.file.processing_fingerprint,
                )
                or payload.file.expires_at <= payload.file.created_at
            ):
                raise ValueError("binding")
            return payload
        except Exception as exc:
            raise PermissionError("csv_file_unavailable") from exc

    def owned(self, db, file_id, actor):
        row = db.execute("SELECT * FROM csv_uploads WHERE file_id=?", (str(file_id),)).fetchone()
        if row is None or row["scope"] != self.scope(actor):
            raise PermissionError("csv_file_unavailable")
        payload = self.decode(row)
        if payload.actor != canonical_actor(actor) or payload.file.expires_at <= self.now():
            raise PermissionError("csv_file_unavailable")
        return row, payload

    def stage(self, payload, idempotency_key, guard, audit):
        if not isinstance(idempotency_key, str) or not re.fullmatch(
            r"[A-Za-z0-9._:-]{1,128}", idempotency_key
        ):
            raise CsvIntakeConflict("csv_idempotency_key_invalid")
        scope = self.scope(payload.actor)
        idem = hmac.digest(
            self.scope_key, b"csv-idem-v1:" + idempotency_key.encode(), "sha256"
        ).hex()
        request_hash = self.request_hash(payload)
        guard()
        with self.db(write=True) as db:
            row = db.execute(
                "SELECT * FROM csv_uploads WHERE scope=? AND idem=?", (scope, idem)
            ).fetchone()
            if row is not None:
                old = self.decode(row)
                if old.file.expires_at <= self.now():
                    raise PermissionError("csv_file_unavailable")
                if row["request_hash"] != request_hash:
                    raise CsvIntakeConflict("csv_idempotency_request_changed")
                guard()
                return dict(row), old
            retained = db.execute(
                "SELECT COUNT(*) FROM csv_uploads WHERE scope=?", (scope,)
            ).fetchone()[0]
            total = db.execute("SELECT COUNT(*) FROM csv_uploads").fetchone()[0]
            if retained >= self.max_files_per_actor or total >= self.max_total_files:
                raise CsvIntakeConflict("csv_retained_capacity_exhausted")
            binding = {
                "file_id": str(payload.file.file_id),
                "scope": scope,
                "idem": idem,
                "request_hash": request_hash,
            }
            audit()
            db.execute(
                "INSERT INTO csv_uploads (file_id,scope,idem,request_hash,body) VALUES (?,?,?,?,?)",
                (
                    *binding.values(),
                    self.seal(payload.model_dump_json().encode(), self.aad(binding)),
                ),
            )
            guard()
            return binding | {"report": None}, payload
