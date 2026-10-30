"""Scoped text artifacts with DLP-before-write, retention and legal holds."""

import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID

from generaldataagent.artifacts.local import LocalArtifactStore
from generaldataagent.contracts import ActorContext, ArtifactRef
from generaldataagent.security.dlp import TextDLPScanner


class ArtifactReadPolicy(StrEnum):
    PROJECT = "project"
    CREATOR = "creator"


_LEGACY_TABLE = "governed_artifacts"
_ACCESS_TABLE = "governed_artifacts_v2"
_BASE_COLUMNS = {
    "artifact_id",
    "task_id",
    "tenant_id",
    "project_id",
    "domains",
    "ref_json",
    "expires_at",
    "held",
    "deleted",
}


@dataclass(frozen=True)
class _AuthorizedRecord:
    table: str
    expires_at: float
    held: bool
    deleted: bool


class ArtifactScopeError(PermissionError):
    """The caller's identity does not cover the store's bound scope."""

    def __init__(self) -> None:
        super().__init__("artifact_store_actor_scope_mismatch")


def require_artifact_scope(store: LocalArtifactStore, actor: ActorContext) -> None:
    """One authorization boundary: does this actor cover this store's scope?

    A plain content-addressed store carries no scope, so nothing can mismatch.
    A governed store is bound to one principal, tenant, project and domain set;
    every caller must match that binding. Project sharing uses the reader's own
    bound store over the same index, not another principal's store. Callers
    previously re-implemented this test and could disagree on which fields they
    compared.
    """
    if not isinstance(store, GovernedArtifactStore):
        return
    bound = store.actor
    if (bound.user_id, bound.tenant_id, bound.project_id) != (
        actor.user_id,
        actor.tenant_id,
        actor.project_id,
    ) or not set(actor.data_domains) <= set(bound.data_domains):
        raise ArtifactScopeError()


class GovernedArtifactStore(LocalArtifactStore):
    """Actor-bound local pilot, injectable through the existing artifact-store port.

    The metadata database must be protected outside the agent-visible content tree.
    Retention removes content, retaining tombstones for audit. Backups and remote traces
    need independent retention controls. Legal holds are a trusted operator port.
    """

    def __init__(
        self,
        root: Path,
        *,
        index_path: Path,
        actor: ActorContext,
        scanner: TextDLPScanner | None = None,
        retention_seconds: int = 86400,
        clock: Callable[[], datetime] | None = None,
        read_policy: ArtifactReadPolicy | str = ArtifactReadPolicy.PROJECT,
    ) -> None:
        try:
            self.read_policy = ArtifactReadPolicy(read_policy)
        except (ValueError, TypeError):
            raise ValueError("unsupported artifact read policy") from None
        if not isinstance(actor, ActorContext) or not actor.user_id.strip():
            raise ValueError("artifact creator identity required")
        if not 1 <= retention_seconds <= 365 * 86400:
            raise ValueError("retention must be between one second and one year")
        if index_path.resolve().is_relative_to(root.resolve()):
            raise ValueError("artifact metadata must be outside the content tree")
        super().__init__(root)
        self.index_path, self.actor = index_path, actor
        self.scanner = scanner or TextDLPScanner()
        self.retention_seconds, self.clock = retention_seconds, clock or (lambda: datetime.now(UTC))
        index_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(index_path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "CREATE TABLE IF NOT EXISTS governed_artifacts ("
                "artifact_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, tenant_id TEXT NOT NULL, "
                "project_id TEXT NOT NULL, domains TEXT NOT NULL, ref_json TEXT NOT NULL, "
                "expires_at REAL NOT NULL, held INTEGER NOT NULL DEFAULT 0, "
                "deleted INTEGER NOT NULL DEFAULT 0)"
            )
            # New records are invisible to an old reader which does not know
            # the access policy. Legacy rows remain byte-for-byte unchanged.
            db.execute(
                "CREATE TABLE IF NOT EXISTS governed_artifacts_v2 ("
                "artifact_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, tenant_id TEXT NOT NULL, "
                "project_id TEXT NOT NULL, domains TEXT NOT NULL, ref_json TEXT NOT NULL, "
                "expires_at REAL NOT NULL, held INTEGER NOT NULL DEFAULT 0, "
                "deleted INTEGER NOT NULL DEFAULT 0, read_policy TEXT NOT NULL, "
                "creator_user_id TEXT NOT NULL)"
            )
            for table, expected in (
                (_LEGACY_TABLE, _BASE_COLUMNS),
                (_ACCESS_TABLE, _BASE_COLUMNS | {"read_policy", "creator_user_id"}),
            ):
                columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                if columns != expected:
                    raise ValueError("unsupported artifact index layout")

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("artifact clock must be timezone-aware")
        return now

    def put_bytes(self, **kwargs: Any) -> ArtifactRef:
        self.scanner.check(kwargs["content"])
        expires = (self._now() + timedelta(seconds=self.retention_seconds)).timestamp()
        ref = super().put_bytes(**kwargs)

        try:
            with closing(sqlite3.connect(self.index_path, timeout=5)) as db, db:
                db.execute(
                    "INSERT INTO governed_artifacts_v2 "
                    "(artifact_id, task_id, tenant_id, project_id, domains, ref_json, expires_at, "
                    "read_policy, creator_user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(ref.artifact_id),
                        str(ref.task_id),
                        self.actor.tenant_id,
                        self.actor.project_id,
                        json.dumps(sorted(set(self.actor.data_domains))),
                        ref.model_dump_json(),
                        expires,
                        self.read_policy.value,
                        self.actor.user_id,
                    ),
                )
        except Exception:
            self._path(ref).unlink(missing_ok=True)
            raise
        return ref

    def _path(self, ref: ArtifactRef) -> Path:
        prefix = "artifact://local/"
        if not ref.uri.startswith(prefix):
            raise ValueError("unsupported artifact backend")
        path = (self.root / ref.uri.removeprefix(prefix)).resolve()
        if self.root not in path.parents:
            raise ValueError("artifact path escapes root")
        return path

    def _authorized_record(self, db: sqlite3.Connection, ref: ArtifactRef) -> _AuthorizedRecord:
        # One statement sees both layouts in one SQLite snapshot. A duplicate
        # identity is ambiguous, never a reason to fall back to legacy sharing.
        rows = db.execute(
            "SELECT 'governed_artifacts_v2', tenant_id, project_id, domains, ref_json, "
            "expires_at, held, deleted, read_policy, creator_user_id "
            "FROM governed_artifacts_v2 WHERE artifact_id = ? UNION ALL "
            "SELECT 'governed_artifacts', tenant_id, project_id, domains, ref_json, "
            "expires_at, held, deleted, 'project', NULL "
            "FROM governed_artifacts WHERE artifact_id = ?",
            (str(ref.artifact_id), str(ref.artifact_id)),
        ).fetchall()
        if len(rows) != 1:
            raise PermissionError("artifact_access_denied")
        table, tenant, project, domains, value, expires, held, deleted, policy, creator = rows[0]
        if (
            (tenant, project) != (self.actor.tenant_id, self.actor.project_id)
            or not set(json.loads(domains)) <= set(self.actor.data_domains)
            or ArtifactRef.model_validate_json(value) != ref
            or policy not in (ArtifactReadPolicy.PROJECT, ArtifactReadPolicy.CREATOR)
            or (table == _ACCESS_TABLE and (not isinstance(creator, str) or not creator.strip()))
            or (policy == ArtifactReadPolicy.CREATOR and creator != self.actor.user_id)
        ):
            raise PermissionError("artifact_access_denied")
        return _AuthorizedRecord(table, expires, held, deleted)

    def read_bytes(self, ref: ArtifactRef) -> bytes:
        with closing(sqlite3.connect(self.index_path, timeout=5)) as db:
            row = self._authorized_record(db, ref)
            if row.deleted or row.expires_at <= self._now().timestamp():
                raise PermissionError("artifact_expired_or_deleted")
            return super().read_bytes(ref)

    def export_bytes(self, ref: ArtifactRef) -> bytes:
        content = self.read_bytes(ref)
        self.scanner.check_export(content, ref.classification)
        return content

    def set_legal_hold(self, ref: ArtifactRef, *, held: bool) -> None:
        if type(held) is not bool:
            raise ValueError("legal hold must be boolean")
        with closing(sqlite3.connect(self.index_path, timeout=5)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = self._authorized_record(db, ref)
            if row.deleted:
                raise PermissionError("artifact_already_deleted")
            db.execute(
                f"UPDATE {row.table} SET held = ? WHERE artifact_id = ?",
                (int(held), str(ref.artifact_id)),
            )

    def discard(self, ref: ArtifactRef) -> bool:
        """Remove an artifact written by a delivery that then failed.

        The index row becomes a tombstone rather than disappearing, so the
        rollback stays visible to audit and the artifact ID is never reused.
        A legal hold blocks it: once an operator has pinned the content, a failed
        package must not erase it silently.
        """
        with closing(sqlite3.connect(self.index_path, timeout=5)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = self._authorized_record(db, ref)
            if row.held:
                raise PermissionError("artifact_hold_blocks_discard")
            db.execute(
                f"UPDATE {row.table} SET deleted = 1 WHERE artifact_id = ?",
                (str(ref.artifact_id),),
            )
        return super().discard(ref)

    def delete_expired(self) -> tuple[UUID, ...]:
        deleted = []
        with closing(sqlite3.connect(self.index_path, timeout=5)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            rows = []
            for table in (_LEGACY_TABLE, _ACCESS_TABLE):
                rows.extend(
                    db.execute(
                        f"SELECT ref_json FROM {table} WHERE tenant_id = ? "
                        "AND project_id = ? AND expires_at <= ? AND held = 0 AND deleted = 0",
                        (self.actor.tenant_id, self.actor.project_id, self._now().timestamp()),
                    ).fetchall()
                )
            for (value,) in rows:
                ref = ArtifactRef.model_validate_json(value)
                try:
                    record = self._authorized_record(db, ref)
                except PermissionError:
                    continue
                path = self._path(ref)
                if path.exists():
                    super().read_bytes(ref)  # Verify immutable content before removing it.
                    path.unlink()
                db.execute(
                    f"UPDATE {record.table} SET deleted = 1 WHERE artifact_id = ?",
                    (str(ref.artifact_id),),
                )
                deleted.append(ref.artifact_id)
        return tuple(deleted)
