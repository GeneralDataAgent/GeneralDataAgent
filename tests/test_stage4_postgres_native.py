import os
from datetime import UTC, datetime, timedelta

import pytest

from generaldataagent.connectors.postgres_native import PostgresNativeDriver, PostgresRelationPolicy
from generaldataagent.contracts import ActorContext
from generaldataagent.evaluation.stage_four_postgres import qualify_postgres
from generaldataagent.security.source import SourceLease


@pytest.mark.skipif(
    not os.environ.get("GDA_TEST_POSTGRES_PORT"), reason="isolated PostgreSQL port not configured"
)
def test_real_postgres_enforces_native_actor_rows_columns_roles_and_policy_drift():
    report = qualify_postgres(
        host="127.0.0.1",
        port=int(os.environ["GDA_TEST_POSTGRES_PORT"]),
        database="stage4_synthetic",
        admin_password=os.environ["GDA_TEST_POSTGRES_PASSWORD"],
    )
    assert report["total"] == 19
    assert report["failed"] == 0, [case for case in report["cases"] if not case["passed"]]
    assert report["server_version_num"] >= 170000
    assert report["production_acceptance"] == "incomplete"


def test_postgres_configuration_and_leases_fail_closed_before_connecting():
    policy = PostgresRelationPolicy(frozenset({"amount"}), "a" * 64)
    base = dict(
        host="db.example",
        port=5432,
        database="analytics",
        source_id="warehouse",
        relations={"orders": policy},
    )
    for changes in (
        {"sslmode": "disable"},
        {"sslmode": "disable", "allow_insecure_local": True},
        {"max_rows": 0},
        {"timeout_seconds": float("nan")},
        {"search_schema": "public; drop table x"},
        {"relations": {}},
        {"port": 0},
        {"scope_relation": "too.many.parts"},
    ):
        with pytest.raises(ValueError):
            PostgresNativeDriver(**{**base, **changes})
    with pytest.raises(ValueError):
        PostgresRelationPolicy(frozenset(), "a" * 64)
    with pytest.raises(ValueError):
        PostgresRelationPolicy(frozenset({"amount"}), "unapproved")
    driver = PostgresNativeDriver(**base)
    now = datetime.now(UTC)
    lease = SourceLease(
        actor=ActorContext(user_id="u", tenant_id="t", project_id="p", data_domains=("d",)),
        source_id="other",
        role="reader",
        credential="fixture-private",
        issued_at=now,
        expires_at=now + timedelta(minutes=1),
        allowed_relations=frozenset({"orders"}),
    )
    with pytest.raises(PermissionError), driver.open_session(lease):
        pass
    with pytest.raises(ValueError):
        qualify_postgres(
            host="production.example", port=5432, database="analytics", admin_password="unused"
        )
