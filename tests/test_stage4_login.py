import base64
import hashlib
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key
from fastapi.testclient import TestClient

from generaldataagent.api.app import create_app
from generaldataagent.contracts import ActorContext
from generaldataagent.security.identity import SQLiteSessionBroker
from generaldataagent.security.login import OIDCCodeExchangeClient, OIDCLoginFlow
from generaldataagent.security.oidc import OIDCSessionAdapter


def test_browser_bound_pkce_login_issues_usable_scoped_session_and_blocks_replay(tmp_path):
    private = generate_private_key(public_exponent=65537, key_size=2048)
    broker = SQLiteSessionBroker(tmp_path / "sessions")
    adapter = OIDCSessionAdapter(
        issuer="https://idp.example",
        audience="gda",
        trusted_keys={"key": private.public_key()},
        broker=broker,
        grant_resolver=lambda sub: ActorContext(
            user_id=sub, tenant_id="t", project_id="p", data_domains=("d",)
        ),
        challenge_path=tmp_path / "challenges",
    )
    query = {}
    calls = []

    def exchange(code, verifier, redirect):
        assert code == "one-use-code" and redirect == "https://service.example/auth/callback"
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        assert challenge == query["code_challenge"][0]
        calls.append(code)
        now = int(datetime.now(UTC).timestamp())
        return jwt.encode(
            dict(
                iss=adapter.issuer,
                aud="gda",
                sub="u",
                iat=now,
                exp=now + 300,
                nonce=query["nonce"][0],
            ),
            private,
            algorithm="RS256",
            headers={"kid": "key"},
        )

    flow = OIDCLoginFlow(
        adapter=adapter,
        authorization_endpoint="https://idp.example/authorize",
        redirect_uri="https://service.example/auth/callback",
        state_path=tmp_path / "states",
        exchange_code=exchange,
    )
    app = create_app(session_broker=broker, oidc_login=flow, production_mode=True)
    client = TestClient(app, base_url="https://service.example")
    login = client.get("/auth/login", follow_redirects=False)
    assert login.status_code == 302
    assert "Secure" in login.headers["set-cookie"] and "HttpOnly" in login.headers["set-cookie"]
    assert login.headers["cache-control"] == "no-store"
    query.update(parse_qs(urlsplit(login.headers["location"]).query))
    callback = "/auth/callback?state=" + query["state"][0] + "&code=one-use-code"
    foreign = TestClient(app, base_url="https://service.example")
    assert foreign.get(callback).status_code == 401
    assert client.get(callback.replace(query["state"][0], "forged")).status_code == 401
    # Failed callback clears browser cookie, so start a new browser transaction.
    login = client.get("/auth/login", follow_redirects=False)
    query.update(parse_qs(urlsplit(login.headers["location"]).query))
    callback = "/auth/callback?state=" + query["state"][0] + "&code=one-use-code"
    completed = client.get(callback)
    assert completed.status_code == 200
    token = completed.json()["access_token"]
    assert (
        client.get(
            "/v1/capabilities",
            headers={
                "authorization": f"Bearer {token}",
                "x-user-id": "u",
                "x-tenant-id": "t",
                "x-project-id": "p",
                "x-data-domain": "d",
            },
        ).status_code
        == 200
    )
    assert client.get(callback).status_code == 401
    assert calls == ["one-use-code"]
    assert "no-store" in completed.headers["cache-control"]
    with pytest.raises(ValueError):
        create_app(oidc_login=flow)
    assert TestClient(create_app()).get("/auth/login").status_code == 503
    assert TestClient(create_app()).get("/auth/callback").status_code == 503
    for endpoint in (
        "http://idp.example/token",
        "https://user:secret@idp.example/token",
        "https://idp.example/token?other=x",
    ):
        with pytest.raises(ValueError):
            OIDCCodeExchangeClient(token_endpoint=endpoint, client_id="gda")
