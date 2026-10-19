"""Pinned service JWTs exchanged for revocable, schedule-bound draft-only sessions.

A workload identity authenticates the service. Its authority to act for a schedule's
owner comes ONLY from the operator's server-side delegation check, never JWT claims.
"""

import json
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import closing
from contextvars import ContextVar
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from pydantic import AwareDatetime

from generaldataagent.business.actions import content_hash
from generaldataagent.business.worker import ScheduleWorkload
from generaldataagent.contracts.models import ContractModel
from generaldataagent.security.identity import (
    SessionIdentity,
    SQLiteSessionBroker,
    _SessionRecord,
)


class _Binding(ContractModel):
    workload: ScheduleWorkload
    subject: str
    credential_expires_at: AwareDatetime


class _WorkloadRecord(_SessionRecord):
    workload_binding: str


class WorkloadSessionBroker(SQLiteSessionBroker):
    """Shared broker which rechecks service delegation on EVERY authentication.

    Human SSO sessions retain their original format. Service records carry an extra
    binding field, so a plain SQLiteSessionBroker rejects them rather than bypassing
    delegation. All processes accepting service sessions must use this broker with
    the same trusted, nonrecursive delegation policy and protected database.
    """

    def __init__(
        self,
        path: Path,
        *,
        allowed: Callable[[ScheduleWorkload, str], bool],
        action_allowed: Callable[[ScheduleWorkload, str, object, bytes | None, bool], bool],
        clock=None,
    ):
        super().__init__(path, clock=clock)
        self.allowed = allowed
        self.action_allowed = action_allowed
        self._issuing: ContextVar[str | None] = ContextVar("workload_session_binding", default=None)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS workload_bindings (digest TEXT PRIMARY KEY, body TEXT)"
            )

    def issue_workload(
        self, workload: ScheduleWorkload, subject: str, credential_expiry: datetime
    ) -> str:
        workload = ScheduleWorkload.model_validate_json(workload.model_dump_json())
        if self.allowed(workload, subject) is not True:
            raise PermissionError("workload_delegation_denied")
        binding = _Binding(
            workload=workload, subject=subject, credential_expires_at=credential_expiry
        )
        digest = content_hash(binding.model_dump(mode="json"))
        now = self._clock()
        if credential_expiry.tzinfo is None or not now < credential_expiry <= now + timedelta(
            seconds=900
        ):
            raise PermissionError("workload_credential_expiry_invalid")
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute(
                "INSERT OR IGNORE INTO workload_bindings VALUES(?,?)",
                (digest, binding.model_dump_json()),
            )
        context = self._issuing.set(digest)
        token = None
        try:
            token = self.issue(
                user_id=workload.user_id,
                tenant_id=workload.tenant_id,
                project_id=workload.project_id,
                data_domains=frozenset(workload.data_domains),
                permissions=frozenset({"business:propose"}),
                ttl_seconds=300,
                not_after=min(workload.expires_at, credential_expiry),
            )
            self.authenticate(token)
            return token
        except Exception:
            if token is not None:
                self.revoke(token)
            raise
        finally:
            self._issuing.reset(context)

    def _save(self, digest: str, identity: SessionIdentity) -> None:
        binding = self._issuing.get()
        if binding is None:
            return super()._save(digest, identity)
        record = _WorkloadRecord(
            user_id=identity.user_id,
            tenant_id=identity.tenant_id,
            project_id=identity.project_id,
            data_domains=tuple(sorted(identity.data_domains)),
            permissions=tuple(sorted(identity.permissions)),
            expires_at=identity.expires_at,
            workload_binding=binding,
        )
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("INSERT INTO sessions VALUES(?,?)", (digest, record.model_dump_json()))

    def _load(self, digest: str) -> SessionIdentity | None:
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            row = db.execute(
                "SELECT identity_json FROM sessions WHERE digest=?", (digest,)
            ).fetchone()
        if row is None:
            return None
        try:
            raw = json.loads(row[0])
            if "workload_binding" not in raw:
                return super()._load(digest)
            record = _WorkloadRecord.model_validate_json(row[0])
            with closing(sqlite3.connect(self.path, timeout=5)) as db:
                row = db.execute(
                    "SELECT body FROM workload_bindings WHERE digest=?", (record.workload_binding,)
                ).fetchone()
            if row is None:
                raise ValueError("missing binding")
            binding = _Binding.model_validate_json(row[0])
            workload = binding.workload
            if (
                content_hash(binding.model_dump(mode="json")) != record.workload_binding
                or (record.user_id, record.tenant_id, record.project_id)
                != (workload.user_id, workload.tenant_id, workload.project_id)
                or set(record.data_domains) != set(workload.data_domains)
                or set(record.permissions) != {"business:propose"}
                or not self._clock()
                < record.expires_at
                <= min(workload.expires_at, binding.credential_expires_at)
                or self.allowed(workload, binding.subject) is not True
            ):
                raise ValueError("invalid or revoked delegation")
            return replace(
                record.identity(),
                business_constraint=lambda intent, content, final: (
                    self.allowed(workload, binding.subject) is True
                    and self.action_allowed(workload, binding.subject, intent, content, final)
                    is True
                ),
            )
        except Exception:
            # Permanent invalidation prevents re-enrollment from reviving an old session.
            self._remove(digest)
            raise PermissionError("workload_session_denied") from None


class ProjectedWorkloadToken:
    """Read a fresh projected/mounted service JWT without retaining it in agent state."""

    def __init__(self, path: Path):
        if not path.is_absolute():
            raise ValueError("operator-configured absolute service credential path required")
        self.path = path

    def __call__(self, workload: ScheduleWorkload) -> str:
        with self.path.open("rb") as stream:
            raw = stream.read(16385)
        if not raw or len(raw) > 16384:
            raise PermissionError("workload_credential_unavailable")
        return raw.decode("utf-8").strip()


class OIDCWorkloadSessionIssuer:
    """RS256-only service identity adapter; no discovery, human nonce or token-selected keys.

    source supplies a mounted projected token or an operator-owned bounded SDK callback.
    This is a trusted worker port, not a public endpoint or approval-grant interface.
    """

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        trusted_keys: Mapping[str, RSAPublicKey],
        broker: WorkloadSessionBroker,
        source: Callable[[ScheduleWorkload], str],
    ):
        if (
            not issuer.startswith("https://")
            or not audience
            or not trusted_keys
            or any(
                not isinstance(key, RSAPublicKey) or key.key_size < 2048
                for key in trusted_keys.values()
            )
        ):
            raise ValueError("pinned HTTPS issuer, audience and strong RSA public keys required")
        self.issuer, self.audience, self.keys = issuer, audience, dict(trusted_keys)
        self.broker, self.source = broker, source

    def __call__(self, workload: ScheduleWorkload) -> str:
        try:
            token = self.source(workload)
            if not isinstance(token, str) or not 1 <= len(token) <= 16384:
                raise ValueError("invalid credential size")
            header = jwt.get_unverified_header(token)
            if (
                header.get("alg") != "RS256"
                or header.get("typ", "JWT") not in {"JWT", "at+jwt"}
                or any(k in header for k in ("jku", "x5u", "jwk", "crit"))
            ):
                raise ValueError("unsupported credential header")
            key = self.keys.get(header.get("kid"))
            if key is None:
                raise ValueError("unknown key")
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=self.issuer,
                audience=self.audience,
                options={"require": ["iss", "aud", "sub", "iat", "exp"]},
            )
            now = self.broker._clock().timestamp()
            if (
                not isinstance(claims["sub"], str)
                or not 1 <= len(claims["sub"]) <= 128
                or type(claims["iat"]) is not int
                or type(claims["exp"]) is not int
                or not 0 < claims["exp"] - claims["iat"] <= 900
                or not claims["iat"] <= now < claims["exp"]
                or claims["aud"] not in (self.audience, [self.audience])
            ):
                raise ValueError("invalid credential claims")
            return self.broker.issue_workload(
                workload, claims["sub"], datetime.fromtimestamp(claims["exp"], UTC)
            )
        except Exception:
            raise PermissionError("workload_authentication_failed") from None
