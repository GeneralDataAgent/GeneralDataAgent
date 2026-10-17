"""PostgreSQL native RLS/column-ACL adapter for short-lived scoped credentials."""

import hashlib
import json
import math
import re
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
from psycopg import sql
from sqlglot import parse_one, tokenize
from sqlglot.tokens import TokenType

from generaldataagent.contracts import ActorContext
from generaldataagent.security.source import NativeScopeProof, SourceLease
from generaldataagent.tools import QueryExecutionError, SqlGuard
from generaldataagent.tools.sqlite import QueryResult


def _qualified(value: str) -> sql.Composed:
    parts = value.split(".")
    if not 1 <= len(parts) <= 2 or any(
        not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", part) for part in parts
    ):
        raise ValueError("one- or two-part PostgreSQL identifier required")
    return sql.SQL(".").join(sql.Identifier(part) for part in parts)


def postgres_policy_digest(connection: psycopg.Connection, relation: str) -> str:
    """Capture from a trusted provisioning connection; never auto-trust an observed policy."""
    rows = connection.execute(
        "SELECT polname::text, polcmd::text, polpermissive, polroles::text, "
        "pg_get_expr(polqual, polrelid), pg_get_expr(polwithcheck, polrelid) "
        "FROM pg_policy WHERE polrelid = %s::regclass ORDER BY polname",
        (relation,),
    ).fetchall()
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class PostgresRelationPolicy:
    allowed_columns: frozenset[str]
    policy_digest: str

    def __post_init__(self):
        if (
            not self.allowed_columns
            or not re.fullmatch(r"[0-9a-f]{64}", self.policy_digest)
            or any(
                not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", column)
                for column in self.allowed_columns
            )
        ):
            raise ValueError("explicit column allowlist and approved native policy digest required")


class PostgresNativeDriver:
    """No credential issuance or admin SQL; the trusted provider supplies an expiring role.

    role_scopes is an operator-owned table keyed by role_name, with user_id/tenant_id/project_id
    and data_domains. Application roles must not be able to modify it. Native data policies are
    pinned by their provisioning-time digest, with FORCE RLS and exact SELECT column privileges.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        database: str,
        source_id: str,
        relations: Mapping[str, PostgresRelationPolicy],
        scope_relation: str = "governance.role_scopes",
        search_schema: str = "public",
        sslmode: str = "verify-full",
        sslrootcert: str | None = None,
        allow_insecure_local: bool = False,
        max_rows: int = 1000,
        timeout_seconds: float = 10,
    ):
        _qualified(scope_relation)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", search_schema):
            raise ValueError("fixed search schema required")
        if sslmode not in {"verify-full", "verify-ca", "require"} and not (
            allow_insecure_local
            and host in {"127.0.0.1", "localhost", "::1"}
            and sslmode == "disable"
        ):
            raise ValueError("TLS required except for explicit loopback development")
        if (
            not relations
            or type(max_rows) is not int
            or max_rows < 1
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 60
            or type(port) is not int
            or not 1 <= port <= 65535
        ):
            raise ValueError("bounded PostgreSQL connection/query configuration required")
        for relation in relations:
            _qualified(relation)
        self.host, self.port, self.database, self.source_id = host, port, database, source_id
        self.relations, self.scope_relation, self.search_schema = (
            dict(relations),
            scope_relation,
            search_schema,
        )
        self.sslmode, self.sslrootcert = sslmode, sslrootcert
        self.max_rows, self.timeout_seconds = max_rows, timeout_seconds

    @contextmanager
    def open_session(self, lease: SourceLease) -> Iterator["_PostgresNativeSession"]:
        if (
            lease.source_id != self.source_id
            or len(lease.role) > 63
            or not lease.allowed_relations <= self.relations.keys()
        ):
            raise PermissionError("postgres_source_grant_denied")
        kwargs = {
            "host": self.host,
            "port": self.port,
            "dbname": self.database,
            "user": lease.role,
            "password": lease.credential.get_secret_value(),
            "connect_timeout": 5,
            "sslmode": self.sslmode,
        }
        if self.sslrootcert is not None:
            kwargs["sslrootcert"] = self.sslrootcert
        try:
            with psycopg.connect(**kwargs) as connection, connection.transaction():
                connection.execute("SET TRANSACTION READ ONLY")
                connection.execute("SET LOCAL row_security = on")
                connection.execute(
                    sql.SQL("SET LOCAL search_path = {}, pg_catalog").format(
                        sql.Identifier(self.search_schema)
                    )
                )
                remaining = (lease.expires_at - datetime.now(UTC)).total_seconds()
                if remaining <= 0:
                    raise PermissionError("postgres_source_lease_expired")
                milliseconds = max(1, int(min(remaining, self.timeout_seconds) * 1000))
                connection.execute(
                    "SELECT set_config('statement_timeout', %s, true)", (str(milliseconds),)
                )
                yield _PostgresNativeSession(connection, self, lease)
        except PermissionError:
            raise
        except Exception:
            raise QueryExecutionError("postgres_scoped_query_failed") from None


class _PostgresNativeSession:
    def __init__(
        self, connection: psycopg.Connection, driver: PostgresNativeDriver, lease: SourceLease
    ):
        self.connection, self.driver, self.lease = connection, driver, lease

    def inspect_native_scope(self) -> NativeScopeProof:
        connection, lease, driver = self.connection, self.lease, self.driver
        role = connection.execute(
            "SELECT current_user::text, rolsuper, rolbypassrls, rolvaliduntil, "
            "current_setting('transaction_read_only'), current_setting('row_security'), "
            "rolcreaterole, rolcreatedb, rolreplication "
            "FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
        if (
            role is None
            or role[0] != lease.role
            or role[1]
            or role[2]
            or role[3] is None
            or not datetime.now(UTC) < role[3] <= lease.expires_at
            or role[4:6] != ("on", "on")
            or any(role[6:])
        ):
            raise PermissionError("postgres_native_role_denied")
        mutable_scope = connection.execute(
            "SELECT has_table_privilege(current_user, %s, 'INSERT, UPDATE, DELETE, TRUNCATE')",
            (driver.scope_relation,),
        ).fetchone()[0]
        if mutable_scope:
            raise PermissionError("postgres_scope_registry_mutable")
        scopes = connection.execute(
            sql.SQL(
                "SELECT user_id, tenant_id, project_id, data_domains FROM {} "
                "WHERE role_name = current_user"
            ).format(_qualified(driver.scope_relation))
        ).fetchall()
        if len(scopes) != 1:
            raise PermissionError("postgres_native_identity_denied")
        user, tenant, project, domains = scopes[0]
        actor = ActorContext(
            user_id=user, tenant_id=tenant, project_id=project, data_domains=tuple(domains)
        )
        if (actor.user_id, actor.tenant_id, actor.project_id, set(actor.data_domains)) != (
            lease.actor.user_id,
            lease.actor.tenant_id,
            lease.actor.project_id,
            set(lease.actor.data_domains),
        ):
            raise PermissionError("postgres_native_identity_denied")
        for relation in sorted(lease.allowed_relations):
            policy = driver.relations[relation]
            connection.execute(
                sql.SQL("SELECT {} FROM {} LIMIT 0").format(
                    sql.SQL(", ").join(
                        sql.Identifier(column) for column in sorted(policy.allowed_columns)
                    ),
                    _qualified(relation),
                )
            )
            flags = connection.execute(
                "SELECT relrowsecurity, relforcerowsecurity, "
                "pg_has_role(current_user, relowner, 'USAGE'), "
                "has_table_privilege(current_user, oid, 'SELECT'), relkind::text "
                "FROM pg_class WHERE oid = %s::regclass",
                (relation,),
            ).fetchone()
            if (
                flags is None
                or not flags[0]
                or not flags[1]
                or flags[2]
                or flags[3]
                or flags[4] not in {"r", "p"}
                or postgres_policy_digest(connection, relation) != policy.policy_digest
            ):
                raise PermissionError("postgres_native_policy_denied")
            columns = connection.execute(
                "SELECT attname::text, "
                "has_column_privilege(current_user, attrelid, attname, 'SELECT') "
                "FROM pg_attribute WHERE attrelid = %s::regclass "
                "AND attnum > 0 AND NOT attisdropped",
                (relation,),
            ).fetchall()
            if {name for name, allowed in columns if allowed} != policy.allowed_columns:
                raise PermissionError("postgres_native_columns_denied")
        return NativeScopeProof(
            actor=lease.actor,
            source_id=driver.source_id,
            role=role[0],
            row_security_enforced=True,
            column_security_enforced=True,
            read_only=True,
            superuser=False,
            bypass_rls=False,
        )

    def execute(self, text: str, parameters: tuple) -> QueryResult:
        started = time.monotonic()
        SqlGuard(dialect="sqlite").inspect(
            text, allowed_relations=set(self.lease.allowed_relations)
        )
        tokens = tokenize(text, read="sqlite")
        placeholders = [token for token in tokens if token.token_type == TokenType.PLACEHOLDER]
        if (
            len(placeholders) != len(parameters)
            or any(token.text != "?" for token in placeholders)
            or any(
                token.token_type != TokenType.STRING and re.fullmatch(r"\$[0-9]+", token.text)
                for token in tokens
            )
        ):
            raise QueryExecutionError("postgres_parameter_binding_denied")
        rendered = text
        for index, token in reversed(tuple(enumerate(placeholders, start=1))):
            rendered = rendered[: token.start] + f"${index}" + rendered[token.end + 1 :]
        expression = parse_one(rendered, read="sqlite")
        # RawCursor uses server-side $n binding, preserving literal '%' and '$1' safely.
        with psycopg.RawCursor(self.connection) as cursor:
            cursor.execute(expression.sql(dialect="postgres"), parameters)
            columns = tuple(item.name for item in (cursor.description or ()))
            rows = tuple(cursor.fetchmany(self.driver.max_rows + 1))
        if len(rows) > self.driver.max_rows:
            raise QueryExecutionError("postgres_query_row_limit")
        return QueryResult(columns, rows, (time.monotonic() - started) * 1000, ())
