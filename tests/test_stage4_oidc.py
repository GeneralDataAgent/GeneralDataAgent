from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key

from generaldataagent.contracts import ActorContext
from generaldataagent.security.identity import SQLiteSessionBroker
from generaldataagent.security.oidc import IdentityGrant, OIDCSessionAdapter


def test_oidc_pinned_keys_claims_nonce_replay_and_server_side_grants(tmp_path):
    private = generate_private_key(public_exponent=65537, key_size=2048)
    other = generate_private_key(public_exponent=65537, key_size=2048)
    broker = SQLiteSessionBroker(tmp_path / "sessions")

    def grants(subject):
        if subject != "trusted-subject":
            raise PermissionError("unknown user")
        return IdentityGrant(
            ActorContext(
                user_id="internal-user", tenant_id="t", project_id="p", data_domains=("d",)
            ),
            frozenset({"analysis:run"}),
        )

    adapter = OIDCSessionAdapter(
        issuer="https://idp.example",
        audience="gda",
        trusted_keys={"key": private.public_key()},
        broker=broker,
        grant_resolver=grants,
        challenge_path=tmp_path / "challenges",
    )
    nonce = adapter.create_challenge()
    now = int(datetime.now(UTC).timestamp())
    claims = dict(
        iss="https://idp.example",
        aud="gda",
        sub="trusted-subject",
        iat=now,
        exp=now + 300,
        nonce=nonce,
        tenant_id="attacker-tenant",
        data_domains=["everything"],
    )

    def signed(changes=None, headers=None, signer=private):
        return jwt.encode(
            {**claims, **(changes or {})},
            signer,
            algorithm="RS256",
            headers={"kid": "key", **(headers or {})},
        )

    for changes in (
        {"iss": "https://evil.example"},
        {"aud": "other"},
        {"sub": "unknown"},
        {"exp": now - 1},
        {"iat": now + 100},
        {"nonce": "other"},
        {"azp": "other"},
        {"aud": ["gda", "other"]},
        {"iat": now - 400},
        {"exp": now + 4000},
    ):
        with pytest.raises(PermissionError, match="oidc_authentication_failed"):
            adapter.exchange(signed(changes), expected_nonce=nonce)
    for headers in ({"kid": "unknown"}, {"jku": "https://evil.example/key"}, {"crit": ["x"]}):
        with pytest.raises(PermissionError):
            adapter.exchange(signed(headers=headers), expected_nonce=nonce)
    with pytest.raises(PermissionError):
        adapter.exchange(signed(signer=other), expected_nonce=nonce)
    with pytest.raises(PermissionError):
        adapter.exchange(
            jwt.encode(claims, "a" * 32, algorithm="HS256", headers={"kid": "key"}),
            expected_nonce=nonce,
        )

    def exchange(_):
        try:
            return adapter.exchange(signed(), expected_nonce=nonce)
        except PermissionError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        tokens = [value for value in pool.map(exchange, range(16)) if value]
    assert len(tokens) == 1
    identity = broker.authenticate(tokens[0])
    assert identity.tenant_id == "t" and identity.data_domains == frozenset({"d"})
    assert identity.permissions == frozenset({"analysis:run"})
    with pytest.raises(PermissionError):
        adapter.exchange(signed(), expected_nonce=nonce)
    with pytest.raises(ValueError):
        OIDCSessionAdapter(
            issuer="http://unsafe",
            audience="gda",
            trusted_keys={},
            broker=broker,
            grant_resolver=grants,
            challenge_path=tmp_path / "bad",
        )
