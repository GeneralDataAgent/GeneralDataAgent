from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key

from generaldataagent.contracts import ActorContext
from generaldataagent.security.identity import SessionBroker
from generaldataagent.security.oidc import OIDCSessionAdapter


def test_session_hard_deadline_never_rounds_or_extends_token_expiry():
    now = datetime(2026, 10, 18, tzinfo=UTC)
    broker = SessionBroker(clock=lambda: now)
    scope = dict(user_id="u", tenant_id="t", project_id="p", data_domains=frozenset({"d"}))
    deadline = now + timedelta(microseconds=100000)
    token = broker.issue(**scope, not_after=deadline)
    assert broker.authenticate(token).expires_at == deadline
    now = deadline
    with pytest.raises(PermissionError):
        broker.authenticate(token)
    for invalid in (now, now - timedelta(seconds=1), datetime(2026, 1, 1)):
        with pytest.raises(ValueError):
            broker.issue(**scope, not_after=invalid)
    assert broker.authenticate(
        broker.issue(**scope, not_after=now + timedelta(days=1))
    ).expires_at == now + timedelta(minutes=5)


def test_oidc_grant_resolution_cannot_issue_a_session_after_token_expiry(tmp_path, monkeypatch):
    now = datetime.now(UTC)
    clock = [now]
    broker = SessionBroker(clock=lambda: clock[0])
    private = generate_private_key(public_exponent=65537, key_size=2048)

    class Clock:
        @staticmethod
        def now(tz):
            return clock[0]

        @staticmethod
        def fromtimestamp(value, tz):
            return datetime.fromtimestamp(value, tz)

    monkeypatch.setattr("generaldataagent.security.oidc.datetime", Clock)

    def slow_grants(_):
        clock[0] += timedelta(seconds=61)
        return ActorContext(user_id="u", tenant_id="t", project_id="p", data_domains=("d",))

    adapter = OIDCSessionAdapter(
        issuer="https://idp.example",
        audience="gda",
        trusted_keys={"key": private.public_key()},
        broker=broker,
        grant_resolver=slow_grants,
        challenge_path=tmp_path / "nonces",
    )
    nonce = adapter.create_challenge()
    claims = dict(
        iss=adapter.issuer,
        aud="gda",
        sub="u",
        iat=int(now.timestamp()),
        exp=int(now.timestamp()) + 60,
        nonce=nonce,
    )
    token = jwt.encode(claims, private, algorithm="RS256", headers={"kid": "key"})
    with pytest.raises(PermissionError, match="oidc_authentication_failed"):
        adapter.exchange(token, expected_nonce=nonce)
    assert broker._sessions == {}
