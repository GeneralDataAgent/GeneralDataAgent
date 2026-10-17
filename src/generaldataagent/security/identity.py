"""Short-lived opaque sessions issued after an external identity provider authenticates users."""

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock


@dataclass(frozen=True)
class SessionIdentity:
    user_id: str
    tenant_id: str
    project_id: str
    data_domains: frozenset[str]
    expires_at: datetime


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
    ) -> str:
        if not all(value.strip() for value in (user_id, tenant_id, project_id)):
            raise ValueError("identity scope must be nonempty")
        if not data_domains or any(not domain.strip() for domain in data_domains):
            raise ValueError("authorized data domains are required")
        if not 1 <= ttl_seconds <= 900:
            raise ValueError("session lifetime must be between 1 and 900 seconds")
        token = secrets.token_urlsafe(32)
        identity = SessionIdentity(
            user_id,
            tenant_id,
            project_id,
            data_domains,
            self._clock() + timedelta(seconds=ttl_seconds),
        )
        with self._lock:
            self._save(self._digest(token), identity)
        return token

    def authenticate(self, token: str) -> SessionIdentity:
        with self._lock:
            identity = self._load(self._digest(token))
            if identity is None or identity.expires_at <= self._clock():
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
            value = json.loads(row[0])
            return SessionIdentity(
                user_id=value["user_id"],
                tenant_id=value["tenant_id"],
                project_id=value["project_id"],
                data_domains=frozenset(value["data_domains"]),
                expires_at=datetime.fromisoformat(value["expires_at"]),
            )
        except (ValueError, TypeError, KeyError) as exc:
            raise PermissionError("invalid_session_record") from exc

    def _remove(self, digest: str) -> None:
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("DELETE FROM sessions WHERE digest = ?", (digest,))
