"""Browser-bound OIDC authorization-code/PKCE controller ports."""

import base64
import hashlib
import json
import secrets
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import SecretStr

from generaldataagent.security.oidc import OIDCSessionAdapter


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _https_url(value: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.query
    ):
        raise ValueError("fixed HTTPS endpoint without credentials/query/fragment required")


class OIDCCodeExchangeClient:
    """Fixed HTTPS token endpoint, bounded response, no redirect or credential logging."""

    def __init__(
        self, *, token_endpoint: str, client_id: str, client_secret: SecretStr | None = None
    ):
        _https_url(token_endpoint)
        self.endpoint, self.client_id, self.secret = token_endpoint, client_id, client_secret

    def __call__(self, code: str, verifier: str, redirect_uri: str) -> str:
        value = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.client_id,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
        }
        if self.secret is not None:
            value["client_secret"] = self.secret.get_secret_value()
        request = Request(
            self.endpoint,
            data=urlencode(value).encode(),
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with build_opener(_NoRedirect()).open(request, timeout=10) as response:
                content = response.read(65537)
                if len(content) > 65536:
                    raise ValueError("token response too large")
                token = json.loads(content)["id_token"]
                if not isinstance(token, str):
                    raise ValueError("missing identity token")
                return token
        except Exception:
            raise PermissionError("oidc_code_exchange_failed") from None


class OIDCLoginFlow:
    def __init__(
        self,
        *,
        adapter: OIDCSessionAdapter,
        authorization_endpoint: str,
        redirect_uri: str,
        state_path: Path,
        exchange_code: Callable[[str, str, str], str],
    ) -> None:
        _https_url(authorization_endpoint)
        _https_url(redirect_uri)
        self.adapter, self.authorization_endpoint = adapter, authorization_endpoint
        self.redirect_uri, self.state_path, self.exchange_code = (
            redirect_uri,
            state_path,
            exchange_code,
        )
        state_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(state_path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS login_states ("
                "state_digest TEXT PRIMARY KEY, cookie_digest TEXT NOT NULL, nonce TEXT NOT NULL, "
                "verifier TEXT NOT NULL, expires REAL NOT NULL, consumed INTEGER NOT NULL)"
            )

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    def begin(self) -> tuple[str, str]:
        state, cookie, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        nonce = self.adapter.create_challenge()
        now = datetime.now(UTC).timestamp()
        with closing(sqlite3.connect(self.state_path, timeout=5)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM login_states WHERE expires <= ?", (now,))
            if db.execute("SELECT COUNT(*) FROM login_states").fetchone()[0] >= 1000:
                raise PermissionError("login_capacity_exceeded")
            db.execute(
                "INSERT INTO login_states VALUES (?, ?, ?, ?, ?, 0)",
                (self._digest(state), self._digest(cookie), nonce, verifier, now + 300),
            )
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        query = urlencode(
            {
                "client_id": self.adapter.audience,
                "response_type": "code",
                "scope": "openid",
                "redirect_uri": self.redirect_uri,
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self.authorization_endpoint}?{query}", cookie

    def finish(self, *, state: str, cookie: str, code: str) -> str:
        try:
            if (
                not state
                or not cookie
                or not code
                or max(len(state), len(cookie), len(code)) > 4096
            ):
                raise ValueError("invalid login callback")
            with closing(sqlite3.connect(self.state_path, timeout=5)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT nonce, verifier FROM login_states WHERE state_digest = ? "
                    "AND cookie_digest = ? AND expires > ? AND consumed = 0",
                    (self._digest(state), self._digest(cookie), datetime.now(UTC).timestamp()),
                ).fetchone()
                if row is None:
                    raise ValueError("login state mismatch")
                db.execute(
                    "UPDATE login_states SET consumed = 1, verifier = '' WHERE state_digest = ?",
                    (self._digest(state),),
                )
            token = self.exchange_code(code, row[1], self.redirect_uri)
            return self.adapter.exchange(token, expected_nonce=row[0])
        except Exception:
            raise PermissionError("oidc_login_failed") from None
