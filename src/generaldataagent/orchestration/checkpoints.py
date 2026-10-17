"""Encrypted, actor-bound SQLite checkpoints with explicit retention."""

import hashlib
import hmac
import json
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

from generaldataagent.contracts import ActorContext


class _EncryptedSerializer:
    def __init__(self, key: bytes):
        self.cipher = AESGCM(key)
        self.inner = JsonPlusSerializer(pickle_fallback=False)

    def dumps_typed(self, value: Any) -> tuple[str, bytes]:
        kind, raw = self.inner.dumps_typed(value)
        nonce = os.urandom(12)
        return f"gda-aesgcm-v1:{kind}", nonce + self.cipher.encrypt(nonce, raw, kind.encode())

    def loads_typed(self, value: tuple[str, bytes]) -> Any:
        kind, raw = value
        if not kind.startswith("gda-aesgcm-v1:"):
            raise ValueError("plaintext checkpoint rejected")
        inner = kind.removeprefix("gda-aesgcm-v1:")
        return self.inner.loads_typed(
            (inner, self.cipher.decrypt(raw[:12], raw[12:], inner.encode()))
        )


class ProtectedSqliteSaver(SqliteSaver):
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        encryption_key: bytes,
        clock: Callable[[], datetime] | None = None,
    ):
        if len(encryption_key) != 32:
            raise ValueError("checkpoint AES-256 key must contain exactly 32 bytes")
        super().__init__(connection, serde=_EncryptedSerializer(encryption_key))
        self.scope_key = hmac.new(
            encryption_key, b"gda-checkpoint-scope-v1", hashlib.sha256
        ).digest()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.setup()
        with self.lock, self.conn:
            self.conn.execute("PRAGMA secure_delete = ON")
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS protected_threads ("
                "thread_id TEXT PRIMARY KEY, scope_digest TEXT NOT NULL, "
                "expires REAL NOT NULL, held INTEGER NOT NULL DEFAULT 0, "
                "deleted INTEGER NOT NULL DEFAULT 0, scope_type TEXT, scope_cipher BLOB)"
            )

    def hold_thread(self, thread_id: str, *, held: bool) -> None:
        """Trusted operator-only port; deliberately absent from the user API."""
        if type(held) is not bool:
            raise ValueError("legal hold must be boolean")
        with self.lock, self.conn:
            changed = self.conn.execute(
                "UPDATE protected_threads SET held = ? WHERE thread_id = ? AND deleted = 0",
                (int(held), thread_id),
            )
            if changed.rowcount != 1:
                raise PermissionError("checkpoint_access_denied")

    def _scope(self, actor: ActorContext) -> str:
        value = actor.model_dump(mode="json")
        value["data_domains"] = sorted(set(value["data_domains"]))
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return hmac.new(self.scope_key, canonical, hashlib.sha256).hexdigest()

    def register_thread(
        self, thread_id: str, actor: ActorContext, *, retention_seconds: int = 86400
    ) -> None:
        if str(UUID(thread_id)) != thread_id or not 1 <= retention_seconds <= 365 * 86400:
            raise ValueError("canonical UUID and bounded retention required")
        with self.lock, self.conn:
            try:
                self.conn.execute(
                    "INSERT INTO protected_threads "
                    "(thread_id, scope_digest, expires, scope_type, scope_cipher) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        thread_id,
                        self._scope(actor),
                        self.clock().timestamp() + retention_seconds,
                        *self.serde.dumps_typed(actor.model_dump(mode="json")),
                    ),
                )
            except sqlite3.IntegrityError:
                raise PermissionError("thread_already_registered_use_resume") from None

    def authorize_thread(self, thread_id: str, actor: ActorContext) -> ActorContext:
        with self.lock:
            row = self.conn.execute(
                "SELECT scope_digest, expires, deleted, scope_type, scope_cipher "
                "FROM protected_threads WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
        if row is None or row[1] <= self.clock().timestamp() or row[2]:
            raise PermissionError("checkpoint_access_denied")
        try:
            original = ActorContext.model_validate(self.serde.loads_typed((row[3], row[4])))
        except Exception:
            raise PermissionError("checkpoint_access_denied") from None
        if (
            row[0] != self._scope(original)
            or (original.user_id, original.tenant_id, original.project_id)
            != (actor.user_id, actor.tenant_id, actor.project_id)
            or not set(original.data_domains) <= set(actor.data_domains)
        ):
            raise PermissionError("checkpoint_access_denied")
        return original

    def put(self, config, checkpoint, metadata, new_versions):
        # The upstream saver stores metadata as plaintext JSON; keep only fixed routing metadata.
        safe_config = {
            "configurable": {
                key: value
                for key, value in config["configurable"].items()
                if key in {"thread_id", "checkpoint_ns", "checkpoint_id"}
            }
        }
        safe_metadata = {
            "source": metadata.get("source")
            if metadata.get("source") in {"input", "loop", "update", "fork"}
            else "loop",
            "step": metadata.get("step") if type(metadata.get("step")) is int else 0,
            "parents": {},
        }
        return super().put(safe_config, checkpoint, safe_metadata, new_versions)

    def purge_expired(self) -> int:
        with self.lock, self.conn:
            rows = self.conn.execute(
                "SELECT thread_id FROM protected_threads WHERE expires <= ? "
                "AND held = 0 AND deleted = 0",
                (self.clock().timestamp(),),
            ).fetchall()
            for (thread_id,) in rows:
                self.conn.execute("DELETE FROM checkpoints WHERE thread_id = ?", (thread_id,))
                self.conn.execute("DELETE FROM writes WHERE thread_id = ?", (thread_id,))
                self.conn.execute(
                    "UPDATE protected_threads SET deleted = 1 WHERE thread_id = ?", (thread_id,)
                )
        return len(rows)


@contextmanager
def open_protected_checkpointer(
    path: Path, *, encryption_key: bytes, clock: Callable[[], datetime] | None = None
) -> Iterator[ProtectedSqliteSaver]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, check_same_thread=False)) as connection:
        yield ProtectedSqliteSaver(connection, encryption_key=encryption_key, clock=clock)
