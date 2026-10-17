from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest

from generaldataagent.contracts import ActorContext
from generaldataagent.security.source import NativeScopeProof, ScopedQueryExecutor, SourceLease
from generaldataagent.tools import QueryExecutionError
from generaldataagent.tools.sqlite import QueryResult


def test_short_lease_and_live_native_rls_cls_scope_are_required_before_query():
    now = datetime.now(UTC)
    actor = ActorContext(user_id="u", tenant_id="t", project_id="p", data_domains=("d",))
    lease = SourceLease(
        actor=actor,
        source_id="warehouse",
        role="scoped-reader",
        credential="private-token",
        issued_at=now,
        expires_at=now + timedelta(minutes=5),
        allowed_relations=frozenset({"orders"}),
    )
    proof = NativeScopeProof(
        actor=actor,
        source_id="warehouse",
        role="scoped-reader",
        row_security_enforced=True,
        column_security_enforced=True,
        read_only=True,
        superuser=False,
        bypass_rls=False,
    )
    calls, closed = [], []

    class Session:
        def inspect_native_scope(self):
            return proof

        def execute(self, sql, params):
            calls.append((sql, params))
            return QueryResult(("n",), ((1,),), 1.0, ())

    @contextmanager
    def opened(value):
        assert value.credential.get_secret_value() == "private-token"
        try:
            yield Session()
        finally:
            closed.append(True)

    executor = ScopedQueryExecutor(
        lease_provider=lambda _: lease, open_session=opened, clock=lambda: now
    )
    assert executor.execute_for_actor("SELECT COUNT(*) AS n FROM orders", (), actor).rows == ((1,),)
    assert len(closed) == 1
    for changes in (
        {"row_security_enforced": False},
        {"column_security_enforced": False},
        {"read_only": False},
        {"superuser": True},
        {"bypass_rls": True},
        {"role": "administrator"},
        {"source_id": "other"},
        {"actor": actor.model_copy(update={"tenant_id": "other"})},
    ):
        original = proof
        proof = proof.model_copy(update=changes)
        with pytest.raises(PermissionError):
            executor.execute_for_actor("SELECT COUNT(*) FROM orders", (), actor)
        proof = original
    assert len(calls) == 1
    for changes in (
        {"expires_at": now},
        {"expires_at": now + timedelta(seconds=901)},
        {"issued_at": now + timedelta(seconds=1)},
        {"actor": actor.model_copy(update={"project_id": "other"})},
    ):
        original = lease
        lease = lease.model_copy(update=changes)
        with pytest.raises(PermissionError):
            executor.execute_for_actor("SELECT 1 FROM orders", (), actor)
        lease = original
    with pytest.raises(QueryExecutionError):
        executor.execute_for_actor("SELECT * FROM restricted", (), actor)
    assert "private-token" not in repr(lease)
    assert len(calls) == 1


def test_source_errors_do_not_echo_credentials():
    actor = ActorContext(user_id="u", tenant_id="t", project_id="p", data_domains=("d",))

    def broken(_):
        raise RuntimeError("private-credential-from-provider")

    executor = ScopedQueryExecutor(lease_provider=broken, open_session=lambda _: None)
    with pytest.raises(QueryExecutionError) as error:
        executor.execute_for_actor("SELECT 1", (), actor)
    assert str(error.value) == "scoped_source_query_failed"
