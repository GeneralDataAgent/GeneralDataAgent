"""Short-lived credential and native database permission propagation boundary."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr

from generaldataagent.contracts import ActorContext
from generaldataagent.tools import QueryExecutionError, SqlGuard
from generaldataagent.tools.sqlite import QueryResult


class SourceLease(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    actor: ActorContext
    source_id: str = Field(min_length=1, max_length=128)
    role: str = Field(min_length=1, max_length=128)
    credential: SecretStr
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    allowed_relations: frozenset[str] = Field(min_length=1)


class NativeScopeProof(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    actor: ActorContext
    source_id: str
    role: str
    row_security_enforced: bool
    column_security_enforced: bool
    read_only: bool
    superuser: bool
    bypass_rls: bool


class NativeQuerySession(Protocol):
    def inspect_native_scope(self) -> NativeScopeProof: ...
    def execute(self, sql: str, parameters: tuple) -> QueryResult: ...


class ScopedQueryExecutor:
    """A trusted native driver must bind a lease and attest its live database session.

    This does not emulate RLS with generated WHERE clauses. Native policies, credential
    issuance and the driver probe must be independently qualified on the real source.
    The built-in SQLite/DuckDB adapters cannot supply this native-security proof.
    """

    def __init__(
        self,
        *,
        lease_provider: Callable[[ActorContext], SourceLease],
        open_session: Callable[[SourceLease], AbstractContextManager[NativeQuerySession]],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.lease_provider, self.open_session = lease_provider, open_session
        self.clock = clock or (lambda: datetime.now(UTC))

    def execute_for_actor(self, sql: str, parameters: tuple, actor: ActorContext) -> QueryResult:
        try:
            lease = self.lease_provider(actor)
            now = self.clock()
            if (
                lease.actor != actor
                or now.tzinfo is None
                or not lease.issued_at <= now < lease.expires_at
                or not 0 < (lease.expires_at - lease.issued_at).total_seconds() <= 900
                or not lease.credential.get_secret_value()
            ):
                raise PermissionError("source_lease_denied")
            SqlGuard().inspect(sql, allowed_relations=set(lease.allowed_relations))
            with self.open_session(lease) as session:
                proof = session.inspect_native_scope()
                if (
                    proof.actor != actor
                    or proof.source_id != lease.source_id
                    or proof.role != lease.role
                    or not proof.row_security_enforced
                    or not proof.column_security_enforced
                    or not proof.read_only
                    or proof.superuser
                    or proof.bypass_rls
                    or self.clock() >= lease.expires_at
                ):
                    raise PermissionError("native_source_scope_denied")
                result = session.execute(sql, parameters)
                if self.clock() >= lease.expires_at:
                    raise PermissionError("source_lease_expired_during_execution")
                return result
        except PermissionError:
            raise PermissionError("scoped_source_access_denied") from None
        except Exception:
            raise QueryExecutionError("scoped_source_query_failed") from None
