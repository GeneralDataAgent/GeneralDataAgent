"""Scoped text artifacts with DLP-before-write, retention and legal holds."""

import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from generaldataagent.artifacts.local import LocalArtifactStore
from generaldataagent.contracts import ActorContext, ArtifactRef
from generaldataagent.security.dlp import TextDLPScanner


class ArtifactScopeError(PermissionError):
    """The caller's identity does not cover the store's bound scope."""

    def __init__(self) -> None:
        super().__init__("artifact_store_actor_scope_mismatch")


def require_artifact_scope(store: LocalArtifactStore, actor: ActorContext) -> None:
    """One authorization boundary: does this actor cover this store's scope?

    A plain content-addressed store carries no scope, so nothing can mismatch.
    A governed store is bound to one tenant, project and domain set; every
    caller that writes or reads through it must match that binding. Callers
    previously re-implemented this test and could disagree on which fields they
    compared.
    """
    if not isinstance(store, GovernedArtifactStore):
        return
    bound = store.actor
    if (bound.tenant_id, bound.project_id) != (
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
    ) -> None:
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
            db.execute(
                "CREATE TABLE IF NOT EXISTS governed_artifacts ("
                "artifact_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, tenant_id TEXT NOT NULL, "
                "project_id TEXT NOT NULL, domains TEXT NOT NULL, ref_json TEXT NOT NULL, "
                "expires_at REAL NOT NULL, held INTEGER NOT NULL DEFAULT 0, "
                "deleted INTEGER NOT NULL DEFAULT 0)"
            )

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
                    "INSERT INTO governed_artifacts "
                    "(artifact_id, task_id, tenant_id, project_id, domains, ref_json, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(ref.artifact_id),
                        str(ref.task_id),
                        self.actor.tenant_id,
                        self.actor.project_id,
                        json.dumps(sorted(set(self.actor.data_domains))),
                        ref.model_dump_json(),
                        expires,
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

    def _authorized_record(self, db: sqlite3.Connection, ref: ArtifactRef) -> tuple:

        row = db.execute(
            "SELECT tenant_id, project_id, domains, ref_json, expires_at, held, deleted "
            "FROM governed_artifacts WHERE artifact_id = ?",
            (str(ref.artifact_id),),
        ).fetchone()
        if (
            row is None
            or row[:2] != (self.actor.tenant_id, self.actor.project_id)
            or not set(json.loads(row[2])) <= set(self.actor.data_domains)
            or ArtifactRef.model_validate_json(row[3]) != ref
        ):
            raise PermissionError("artifact_access_denied")
        return row

    def read_bytes(self, ref: ArtifactRef) -> bytes:
        with closing(sqlite3.connect(self.index_path, timeout=5)) as db:
            row = self._authorized_record(db, ref)
            if row[6] or row[4] <= self._now().timestamp():
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
            if row[6]:
                raise PermissionError("artifact_already_deleted")
            db.execute(
                "UPDATE governed_artifacts SET held = ? WHERE artifact_id = ?",
                (int(held), str(ref.artifact_id)),
            )

    def delete_expired(self) -> tuple[UUID, ...]:
        deleted = []
        with closing(sqlite3.connect(self.index_path, timeout=5)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT ref_json FROM governed_artifacts WHERE tenant_id = ? "
                "AND project_id = ? AND expires_at <= ? AND held = 0 AND deleted = 0",
                (self.actor.tenant_id, self.actor.project_id, self._now().timestamp()),
            ).fetchall()
            for (value,) in rows:
                ref = ArtifactRef.model_validate_json(value)
                try:
                    self._authorized_record(db, ref)
                except PermissionError:
                    continue
                path = self._path(ref)
                if path.exists():
                    super().read_bytes(ref)  # Verify immutable content before removing it.
                    path.unlink()
                db.execute(
                    "UPDATE governed_artifacts SET deleted = 1 WHERE artifact_id = ?",
                    (str(ref.artifact_id),),
                )
                deleted.append(ref.artifact_id)
        return tuple(deleted)
