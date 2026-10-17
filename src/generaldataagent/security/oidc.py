"""Pinned-key OIDC ID-token adapter for a trusted authorization-code callback."""

import hashlib
import secrets
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

from generaldataagent.contracts import ActorContext
from generaldataagent.security.identity import SessionBroker


@dataclass(frozen=True)
class IdentityGrant:
    actor: ActorContext
    permissions: frozenset[str]


class OIDCSessionAdapter:
    """RS256-only pilot with server-side grant resolution and one-use nonce checks.

    The web login controller must bind the challenge to its browser transaction,
    validate OAuth state/PKCE and exchange the code over TLS before calling exchange.
    There is no public token-exchange endpoint and no token-selected discovery URL.
    Keys and grants are provisioned by trusted operators, never by JWT claims.
    """

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        trusted_keys: Mapping[str, RSAPublicKey],
        broker: SessionBroker,
        grant_resolver: Callable[[str], ActorContext | IdentityGrant],
        challenge_path: Path,
    ) -> None:
        if (
            not issuer.startswith("https://")
            or not audience
            or not trusted_keys
            or any(key.key_size < 2048 for key in trusted_keys.values())
        ):
            raise ValueError("trusted HTTPS issuer, audience and RSA keys are required")
        self.issuer, self.audience, self.keys = issuer, audience, dict(trusted_keys)
        self.broker, self.grant_resolver, self.challenge_path = (
            broker,
            grant_resolver,
            challenge_path,
        )
        challenge_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(challenge_path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS oidc_challenges ("
                "digest TEXT PRIMARY KEY, expires REAL NOT NULL, consumed INTEGER NOT NULL)"
            )

    def create_challenge(self) -> str:
        nonce = secrets.token_urlsafe(32)
        with closing(sqlite3.connect(self.challenge_path, timeout=5)) as db, db:
            db.execute(
                "DELETE FROM oidc_challenges WHERE expires <= ? OR consumed = 1",
                (datetime.now(UTC).timestamp(),),
            )
            db.execute(
                "INSERT INTO oidc_challenges VALUES (?, ?, 0)",
                (hashlib.sha256(nonce.encode()).hexdigest(), datetime.now(UTC).timestamp() + 300),
            )
        return nonce

    def exchange(self, id_token: str, *, expected_nonce: str) -> str:
        try:
            if len(id_token) > 16384 or len(expected_nonce) > 128 or not expected_nonce:
                raise ValueError("invalid token size")
            header = jwt.get_unverified_header(id_token)
            if (
                header.get("alg") != "RS256"
                or header.get("typ", "JWT") != "JWT"
                or any(key in header for key in ("jku", "x5u", "jwk", "crit"))
            ):
                raise ValueError("unsupported token header")
            key = self.keys.get(header.get("kid"))
            if key is None:
                raise ValueError("unknown signing key")
            claims = jwt.decode(
                id_token,
                key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer,
                options={
                    "require": ["iss", "aud", "sub", "exp", "iat", "nonce"],
                    "strict_aud": True,
                },
            )
            now = datetime.now(UTC).timestamp()
            if (
                not isinstance(claims["sub"], str)
                or not 1 <= len(claims["sub"]) <= 128
                or type(claims["iat"]) is not int
                or type(claims["exp"]) is not int
                or not 0 < claims["exp"] - claims["iat"] <= 3600
                or not 0 <= now - claims["iat"] <= 300
                or claims.get("azp", self.audience) != self.audience
                or not isinstance(claims["nonce"], str)
                or not secrets.compare_digest(claims["nonce"], expected_nonce)
            ):
                raise ValueError("invalid identity claims")
            grant = self.grant_resolver(claims["sub"])
            actor = grant.actor if isinstance(grant, IdentityGrant) else grant
            permissions = grant.permissions if isinstance(grant, IdentityGrant) else frozenset()
            if not isinstance(actor, ActorContext):
                raise ValueError("trusted identity grant required")
            with closing(sqlite3.connect(self.challenge_path, timeout=5)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                changed = db.execute(
                    "UPDATE oidc_challenges SET consumed = 1 WHERE digest = ? "
                    "AND expires > ? AND consumed = 0",
                    (hashlib.sha256(expected_nonce.encode()).hexdigest(), now),
                )
                if changed.rowcount != 1:
                    raise ValueError("challenge expired or already used")
            return self.broker.issue(
                user_id=actor.user_id,
                tenant_id=actor.tenant_id,
                project_id=actor.project_id,
                data_domains=frozenset(actor.data_domains),
                permissions=permissions,
                ttl_seconds=min(300, max(1, int(claims["exp"] - now))),
            )
        except Exception:
            raise PermissionError("oidc_authentication_failed") from None
