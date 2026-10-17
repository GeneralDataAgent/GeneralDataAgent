import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import SecretStr

from generaldataagent.observability.alerts import CriticalAlert
from generaldataagent.observability.http import AlertHTTPTransport, AuditHTTPTransport
from generaldataagent.security.login import OIDCCodeExchangeClient, _NoRedirect


def test_http_transports_bound_responses_pin_https_and_never_echo_service_secrets(monkeypatch):
    sent = []
    body = b""
    failure = False

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, maximum):
            return body[:maximum]

    class Opener:
        def open(self, request, timeout):
            sent.append(request)
            assert timeout == 10
            if failure:
                raise RuntimeError("private-service-credential")
            return Response()

    monkeypatch.setattr("generaldataagent.observability.http.build_opener", lambda *args: Opener())
    monkeypatch.setattr("generaldataagent.security.login.build_opener", lambda *args: Opener())
    token = SecretStr("fixture-service-credential-1234567890")
    audit = AuditHTTPTransport(endpoint="https://audit.example/v1/append", bearer_token=token)
    event_id = uuid4()
    now = datetime.now(UTC)
    body = json.dumps(
        dict(
            event_id=str(event_id),
            event_sha256="0" * 64,
            sequence=1,
            recorded_at=now.isoformat(),
            signer_id="audit",
            signature="fixture",
        )
    ).encode()
    assert audit(b"{}").event_id == event_id
    assert sent[-1].get_header("Authorization") == "Bearer " + token.get_secret_value()
    alert = CriticalAlert(event_id=event_id, kind="audit_outage", occurred_at=now)
    body = json.dumps(dict(event_id=str(event_id), delivered_at=now.isoformat())).encode()
    assert (
        AlertHTTPTransport(endpoint="https://paging.example/append", bearer_token=token)(
            alert
        ).event_id
        == event_id
    )
    code_client = OIDCCodeExchangeClient(
        token_endpoint="https://idp.example/token", client_id="gda", client_secret=token
    )
    body = b'{"id_token":"fixture-token"}'
    assert (
        code_client("code", "verifier", "https://service.example/auth/callback") == "fixture-token"
    )
    for invalid in (b"private-service-credential", b"x" * 65537):
        body = invalid
        with pytest.raises(PermissionError) as error:
            audit(b"{}")
        assert "private-service-credential" not in str(error.value)
        with pytest.raises(PermissionError):
            code_client("code", "verifier", "https://service.example/auth/callback")
    failure = True
    with pytest.raises(PermissionError, match="audit_append_unavailable"):
        audit(b"{}")
    with pytest.raises(ValueError):
        AuditHTTPTransport(endpoint="http://unsafe", bearer_token=token)
    assert (
        _NoRedirect().redirect_request(None, None, 302, None, None, "https://evil.example") is None
    )
