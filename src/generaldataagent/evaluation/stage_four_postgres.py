"""Real PostgreSQL qualification against an isolated synthetic development database."""

import secrets
from datetime import UTC, datetime, timedelta

import psycopg
from psycopg import sql

from generaldataagent.connectors.postgres_native import (
    PostgresNativeDriver,
    PostgresRelationPolicy,
    postgres_policy_digest,
)
from generaldataagent.contracts import ActorContext
from generaldataagent.security.source import ScopedQueryExecutor, SourceLease
from generaldataagent.tools import QueryExecutionError


def qualify_postgres(*, host: str, port: int, database: str, admin_password: str) -> dict:
    """Creates development fixture tables/roles only in a disposable operator-supplied DB."""
    if host not in {"127.0.0.1", "localhost", "::1"} or database != "stage4_synthetic":
        raise ValueError("qualification only accepts an isolated loopback development database")
    results = []

    def check(case_id, function, expected):
        try:
            value = function()
            actual = "passed" if expected == "passed" and value is not False else "unexpected_allow"
        except (PermissionError, QueryExecutionError):
            actual = "rejected"
        results.append(
            {
                "case_id": case_id,
                "expected": expected,
                "actual": actual,
                "passed": actual == expected,
            }
        )

    with psycopg.connect(
        host=host,
        port=port,
        dbname=database,
        user="postgres",
        password=admin_password,
        sslmode="disable",
        autocommit=True,
    ) as admin:
        server_version = admin.info.server_version
        role_names = ("gda_reader_a", "gda_reader_b")
        passwords = {role: secrets.token_urlsafe(32) for role in role_names}
        expires = datetime.now(UTC) + timedelta(minutes=5)
        for role in role_names:
            admin.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} VALID UNTIL {}").format(
                    sql.Identifier(role),
                    sql.Literal(passwords[role]),
                    sql.Literal(expires.isoformat()),
                )
            )
        admin.execute("CREATE SCHEMA governance")
        admin.execute(
            "CREATE TABLE governance.role_scopes (role_name name PRIMARY KEY, "
            "user_id text NOT NULL, "
            "tenant_id text NOT NULL, project_id text NOT NULL, data_domains text[] NOT NULL)"
        )
        admin.execute(
            "INSERT INTO governance.role_scopes VALUES "
            "('gda_reader_a','u-a','t-a','p-a',ARRAY['commerce']), "
            "('gda_reader_b','u-b','t-b','p-b',ARRAY['commerce'])"
        )
        admin.execute("ALTER TABLE governance.role_scopes ENABLE ROW LEVEL SECURITY")
        admin.execute("ALTER TABLE governance.role_scopes FORCE ROW LEVEL SECURITY")
        admin.execute(
            "CREATE POLICY role_scope ON governance.role_scopes FOR SELECT "
            "USING (role_name = current_user)"
        )
        admin.execute("GRANT USAGE ON SCHEMA governance TO gda_reader_a, gda_reader_b")
        admin.execute("GRANT SELECT ON governance.role_scopes TO gda_reader_a, gda_reader_b")
        admin.execute(
            "CREATE TABLE orders(scope_role name NOT NULL, "
            "amount integer NOT NULL, private_note text)"
        )
        admin.execute(
            "INSERT INTO orders VALUES ('gda_reader_a',10,'private-a'), "
            "('gda_reader_a',20,'private-a'), "
            "('gda_reader_b',99,'private-b')"
        )
        admin.execute("ALTER TABLE orders ENABLE ROW LEVEL SECURITY")
        admin.execute("ALTER TABLE orders FORCE ROW LEVEL SECURITY")
        admin.execute(
            "CREATE POLICY tenant_rows ON orders FOR SELECT USING (scope_role = current_user)"
        )
        admin.execute("GRANT SELECT(amount) ON orders TO gda_reader_a, gda_reader_b")
        digest = postgres_policy_digest(admin, "orders")
        driver = PostgresNativeDriver(
            host=host,
            port=port,
            database=database,
            source_id="postgres-pilot",
            relations={"orders": PostgresRelationPolicy(frozenset({"amount"}), digest)},
            sslmode="disable",
            allow_insecure_local=True,
        )
        actors = {
            "gda_reader_a": ActorContext(
                user_id="u-a", tenant_id="t-a", project_id="p-a", data_domains=("commerce",)
            ),
            "gda_reader_b": ActorContext(
                user_id="u-b", tenant_id="t-b", project_id="p-b", data_domains=("commerce",)
            ),
        }

        def lease(role, actor=None):
            return SourceLease(
                actor=actor or actors[role],
                source_id="postgres-pilot",
                role=role,
                credential=passwords[role],
                issued_at=datetime.now(UTC),
                expires_at=expires,
                allowed_relations=frozenset({"orders"}),
            )

        def execute(role, text, params=(), actor=None, use_driver=driver):
            value = actor or actors[role]
            return ScopedQueryExecutor(
                lease_provider=lambda _: lease(role, value), open_session=use_driver.open_session
            ).execute_for_actor(text, params, value)

        check(
            "tenant_a_rows",
            lambda: (
                execute("gda_reader_a", "SELECT amount FROM orders ORDER BY amount").rows
                == ((10,), (20,))
            ),
            "passed",
        )
        check(
            "tenant_b_rows",
            lambda: execute("gda_reader_b", "SELECT amount FROM orders").rows == ((99,),),
            "passed",
        )
        check(
            "native_aggregate",
            lambda: (
                execute("gda_reader_a", "SELECT SUM(amount) AS total FROM orders").rows == ((30,),)
            ),
            "passed",
        )
        check(
            "bound_parameter",
            lambda: (
                execute("gda_reader_a", "SELECT amount FROM orders WHERE amount >= ?", (20,)).rows
                == ((20,),)
            ),
            "passed",
        )
        check(
            "literal_percent",
            lambda: (
                execute(
                    "gda_reader_a",
                    "SELECT '100%' AS label, amount FROM orders WHERE amount >= ?",
                    (20,),
                ).rows
                == (("100%", 20),)
            ),
            "passed",
        )
        check(
            "cte_parameter_order",
            lambda: (
                execute(
                    "gda_reader_a",
                    "WITH bounds AS (SELECT ? AS lo) SELECT amount FROM orders "
                    "WHERE amount >= (SELECT lo FROM bounds) AND amount <= ?",
                    (20, 30),
                ).rows
                == ((20,),)
            ),
            "passed",
        )
        check(
            "private_column",
            lambda: execute("gda_reader_a", "SELECT private_note FROM orders"),
            "rejected",
        )
        check("star_columns", lambda: execute("gda_reader_a", "SELECT * FROM orders"), "rejected")
        check(
            "cross_actor_credential",
            lambda: execute(
                "gda_reader_a", "SELECT amount FROM orders", actor=actors["gda_reader_b"]
            ),
            "rejected",
        )
        check("write_attempt", lambda: execute("gda_reader_a", "DELETE FROM orders"), "rejected")
        check(
            "scope_registry_read",
            lambda: execute("gda_reader_a", "SELECT * FROM governance.role_scopes"),
            "rejected",
        )
        small = PostgresNativeDriver(
            host=host,
            port=port,
            database=database,
            source_id="postgres-pilot",
            relations=driver.relations,
            sslmode="disable",
            allow_insecure_local=True,
            max_rows=1,
        )
        check(
            "row_limit",
            lambda: execute("gda_reader_a", "SELECT amount FROM orders", use_driver=small),
            "rejected",
        )
        for statement, cleanup, case in [
            (
                "GRANT SELECT ON orders TO gda_reader_a",
                "REVOKE SELECT ON orders FROM gda_reader_a",
                "table_wide_acl",
            ),
            (
                "GRANT SELECT(private_note) ON orders TO gda_reader_a",
                "REVOKE SELECT(private_note) ON orders FROM gda_reader_a",
                "column_acl_drift",
            ),
            (
                "ALTER POLICY tenant_rows ON orders USING (true)",
                "ALTER POLICY tenant_rows ON orders USING (scope_role = current_user)",
                "policy_drift",
            ),
            (
                "ALTER TABLE orders DISABLE ROW LEVEL SECURITY",
                "ALTER TABLE orders ENABLE ROW LEVEL SECURITY",
                "rls_disabled",
            ),
            (
                "ALTER ROLE gda_reader_a BYPASSRLS",
                "ALTER ROLE gda_reader_a NOBYPASSRLS",
                "bypass_role",
            ),
            (
                "GRANT UPDATE ON governance.role_scopes TO gda_reader_a",
                "REVOKE UPDATE ON governance.role_scopes FROM gda_reader_a",
                "mutable_scope_registry",
            ),
        ]:
            admin.execute(statement)
            try:
                check(
                    case, lambda: execute("gda_reader_a", "SELECT amount FROM orders"), "rejected"
                )
            finally:
                admin.execute(cleanup)
                if case == "table_wide_acl":
                    admin.execute("GRANT SELECT(amount) ON orders TO gda_reader_a")
        check(
            "restored_policy",
            lambda: execute("gda_reader_a", "SELECT SUM(amount) FROM orders").rows == ((30,),),
            "passed",
        )
    return {
        "gate_id": "postgres_native_local",
        "server_version_num": server_version,
        "environment": "isolated_synthetic_development",
        "total": len(results),
        "passed": sum(case["passed"] for case in results),
        "failed": sum(not case["passed"] for case in results),
        "cases": results,
        "production_acceptance": "incomplete",
    }
