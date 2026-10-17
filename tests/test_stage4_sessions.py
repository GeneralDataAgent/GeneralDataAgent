from datetime import UTC, datetime, timedelta

import pytest

from generaldataagent.security.identity import SQLiteSessionBroker


def test_shared_session_revocation_restart_and_no_raw_token_storage(tmp_path):
    path = tmp_path / "sessions.db"
    now = datetime.now(UTC)
    first = SQLiteSessionBroker(path, clock=lambda: now)
    second = SQLiteSessionBroker(path, clock=lambda: now)
    token = first.issue(user_id="u", tenant_id="t", project_id="p", data_domains=frozenset({"d"}))
    assert token.encode() not in path.read_bytes()
    assert second.authenticate(token).project_id == "p"
    assert SQLiteSessionBroker(path).authenticate(token).user_id == "u"
    second.revoke(token)
    with pytest.raises(PermissionError):
        first.authenticate(token)
    token = first.issue(
        user_id="u", tenant_id="t", project_id="p", data_domains=frozenset({"d"}), ttl_seconds=2
    )
    now += timedelta(seconds=2)
    with pytest.raises(PermissionError):
        second.authenticate(token)
    with pytest.raises(PermissionError):
        first.authenticate(token)
