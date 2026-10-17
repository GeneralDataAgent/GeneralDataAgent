"""Short-lived opaque sessions issued after an external identity provider authenticates users."""

import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
            self._sessions[self._digest(token)] = identity
        return token

    def authenticate(self, token: str) -> SessionIdentity:
        with self._lock:
            identity = self._sessions.get(self._digest(token))
            if identity is None or identity.expires_at <= self._clock():
                self._sessions.pop(self._digest(token), None)
                raise PermissionError("invalid_or_expired_session")
            return identity

    def revoke(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(self._digest(token), None)

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()
