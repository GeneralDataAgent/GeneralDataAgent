"""Short-lived opaque sessions issued after an external identity provider authenticates users."""

import hashlib
import json
import re
import secrets
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

_ScopeID = Annotated[str, StringConstraints(pattern=r"^[^\s\x00-\x1f\x7f]{1,128}$")]
_Permission = Literal[
    "analysis:run",
    "science:run",
    "actions:resume",
    "reviews:read",
    "reviews:submit",
    "operations:read",
    "business:propose",
    "business:read",
    "approvals:request",
    "approvals:review",
    "approvals:delegate",
]


class _SessionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    user_id: _ScopeID
    tenant_id: _ScopeID
    project_id: _ScopeID
    data_domains: tuple[_ScopeID, ...] = Field(min_length=1, max_length=32)
    expires_at: AwareDatetime
    permissions: tuple[_Permission, ...] = Field(default=(), max_length=8)

    def identity(self) -> "SessionIdentity":
        return SessionIdentity(
            self.user_id,
            self.tenant_id,
            self.project_id,
            frozenset(self.data_domains),
            self.expires_at,
            frozenset(self.permissions),
        )


@dataclass(frozen=True)
class SessionIdentity:
    user_id: str
    tenant_id: str
    project_id: str
    data_domains: frozenset[str]
    expires_at: datetime
    permissions: frozenset[str] = frozenset()
    # Trusted runtime constraint; never accepted from session/JWT claims or persisted as code.
    business_constraint: Callable[[object, bytes | None, bool], bool] | None = None


class SessionBroker:
    """Process-local pilot broker. Only trusted SSO adapters may call issue.

    Tokens are stored as digests, scoped to exactly one project, and revocable.
    A restart invalidates sessions. Multi-worker deployments need a shared broker.
    """

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sessions: dict[str, SessionIdentity] = {}
        self._lock = Lock()

    def issue(
        self,
        *,
        user_id: str,
        tenant_id: str,
        project_id: str,
        data_domains: frozenset[str],
        ttl_seconds: int = 300,
        permissions: frozenset[str] = frozenset(),
        not_after: datetime | None = None,
    ) -> str:
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 900:
            raise ValueError("session lifetime must be between 1 and 900 seconds")
        if not isinstance(data_domains, (set, frozenset)) or not isinstance(
            permissions, (set, frozenset)
        ):
            raise ValueError("domains and permissions must be explicit sets")
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("identity clock must be timezone-aware")
        expires_at = now + timedelta(seconds=ttl_seconds)
        if not_after is not None:
            if not_after.tzinfo is None or not_after <= now:
                raise ValueError("session hard expiry must be aware and in the future")
            expires_at = min(expires_at, not_after)
        identity = _SessionRecord(
            user_id=user_id,
            tenant_id=tenant_id,
            project_id=project_id,
            data_domains=tuple(data_domains),
            permissions=tuple(permissions),
            expires_at=expires_at,
        ).identity()
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._save(self._digest(token), identity)
        return token

    def authenticate(self, token: str) -> SessionIdentity:
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
            raise PermissionError("invalid_or_expired_session")
        now = self._clock()
        if now.tzinfo is None:
            raise PermissionError("invalid_identity_clock")
        with self._lock:
            identity = self._load(self._digest(token))
            if identity is None or identity.expires_at <= now:
                self._remove(self._digest(token))
                raise PermissionError("invalid_or_expired_session")
            return identity

    def revoke(self, token: str) -> None:
        with self._lock:
            self._remove(self._digest(token))

    def _save(self, digest: str, identity: SessionIdentity) -> None:
        self._sessions[digest] = identity

    def _load(self, digest: str) -> SessionIdentity | None:
        return self._sessions.get(digest)

    def _remove(self, digest: str) -> None:
        self._sessions.pop(digest, None)

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()


class SQLiteSessionBroker(SessionBroker):
    """Shared local session/revocation registry for processes using one protected DB.

    The database contains identity scopes and digests, never raw bearer tokens.
    Operators must isolate the database from agents and protect its filesystem permissions.
    """

    def __init__(self, path: Path, *, clock: Callable[[], datetime] | None = None) -> None:
        super().__init__(clock=clock)
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS sessions ("
                "digest TEXT PRIMARY KEY, identity_json TEXT NOT NULL)"
            )

    def _save(self, digest: str, identity: SessionIdentity) -> None:
        value = json.dumps(
            {
                "user_id": identity.user_id,
                "tenant_id": identity.tenant_id,
                "project_id": identity.project_id,
                "data_domains": sorted(identity.data_domains),
                "expires_at": identity.expires_at.isoformat(),
                "permissions": sorted(identity.permissions),
            }
        )
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("INSERT INTO sessions VALUES (?, ?)", (digest, value))

    def _load(self, digest: str) -> SessionIdentity | None:
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            row = db.execute(
                "SELECT identity_json FROM sessions WHERE digest = ?", (digest,)
            ).fetchone()
        if row is None:
            return None
        try:
            return _SessionRecord.model_validate_json(row[0]).identity()
        except (ValueError, TypeError, KeyError):
            raise PermissionError("invalid_session_record") from None

    def _remove(self, digest: str) -> None:
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("DELETE FROM sessions WHERE digest = ?", (digest,))
