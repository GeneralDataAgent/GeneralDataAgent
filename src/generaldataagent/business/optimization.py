"""Opt-in bounded model calls with admission checks and encrypted short-lived caching.

Prices and latency estimates are operator-supplied pinned configuration, not live quotes.
Provider adapters must enforce their own transport deadline and output token bound.
"""

import asyncio
import inspect
import json
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from pydantic import Field

from generaldataagent.business.actions import content_hash
from generaldataagent.business.governance import CapabilityRegistry, actor_scope
from generaldataagent.contracts import ActorContext
from generaldataagent.contracts.models import ContractModel
from generaldataagent.policy.budgets import TaskBudgetLedger
from generaldataagent.security.dlp import TextDLPScanner


class RoutingRequest(ContractModel):
    purpose: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    prompt: str = Field(min_length=1, max_length=16000)
    source_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    prompt_version: str = Field(min_length=1, max_length=128)
    policy_version: str = Field(min_length=1, max_length=128)
    authorization_version: str = Field(min_length=1, max_length=128)
    classification: str = Field(pattern=r"^(public|internal)$")
    input_token_bound: int = Field(ge=1, le=100000)
    output_token_bound: int = Field(ge=1, le=10000)
    max_cost_microunits: int = Field(ge=0)
    max_latency_ms: int = Field(ge=1, le=60000)


class ProviderResult(ContractModel):
    text: str = Field(min_length=1, max_length=32000)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


@dataclass(frozen=True)
class ModelRoute:
    name: str
    version: str
    content_hash: str
    purposes: frozenset[str]
    classifications: frozenset[str]
    input_cost_per_token: int
    output_cost_per_token: int
    estimated_latency_ms: int
    enabled: bool = False

    def __post_init__(self):
        if (
            not self.purposes
            or not self.classifications
            or not self.classifications <= {"public", "internal"}
            or any(
                type(v) is not int or v < 0
                for v in (
                    self.input_cost_per_token,
                    self.output_cost_per_token,
                    self.estimated_latency_ms,
                )
            )
        ):
            raise ValueError("invalid_model_route")

    def cost_bound(self, request):
        return (
            self.input_cost_per_token * request.input_token_bound
            + self.output_cost_per_token * request.output_token_bound
        )


class ScopedResponseCache:
    """Encrypted local cache, max 15-minute TTL, no persistent bearer credentials.

    Caller supplies fresh authorization and source/policy versions on every use. A cache hit
    does not skip capability admission or current task deadline checks in the executor.
    """

    def __init__(self, path: Path, *, key: bytes, clock, ttl_seconds: int = 300):
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 900:
            raise ValueError("invalid_cache_ttl")
        self.path, self.clock, self.ttl_seconds = path, clock, ttl_seconds
        self.cipher = Fernet(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS response_cache ("
                "key TEXT PRIMARY KEY, encrypted BLOB, expires REAL, created REAL)"
            )

    def _now(self):
        now = self.clock()
        if now.tzinfo is None:
            raise PermissionError("cache_requires_aware_clock")
        return now

    @staticmethod
    def cache_key(request: RoutingRequest, actor: ActorContext, route: ModelRoute):
        return content_hash(
            [
                request.model_dump(mode="json"),
                json.loads(actor_scope(actor)),
                route.name,
                route.version,
                route.content_hash,
            ]
        )

    def get(self, key: str) -> ProviderResult | None:
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            now = self._now().timestamp()
            db.execute("DELETE FROM response_cache WHERE expires<=?", (now,))
            row = db.execute(
                "SELECT encrypted,expires,created FROM response_cache WHERE key=?", (key,)
            ).fetchone()
            if row is None:
                return None
            if not row[2] <= now < row[1] or row[1] - row[2] > self.ttl_seconds:
                raise PermissionError("cache_expiry_integrity_failure")
            try:
                body = json.loads(self.cipher.decrypt(row[0]))
                if body["key"] != key or body["expires"] != row[1] or body["created"] != row[2]:
                    raise ValueError("cache_record_mismatch")
                return ProviderResult.model_validate(body["result"])
            except (InvalidToken, ValueError, KeyError, TypeError) as exc:
                raise PermissionError("cache_integrity_failure") from exc

    def put(self, key: str, result: ProviderResult):
        TextDLPScanner().check(result.model_dump_json().encode())
        created = self._now().timestamp()
        expires = created + self.ttl_seconds
        body = json.dumps(
            {
                "key": key,
                "result": result.model_dump(mode="json"),
                "expires": expires,
                "created": created,
            }
        ).encode()
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("DELETE FROM response_cache WHERE expires<=?", (created,))
            db.execute(
                "INSERT INTO response_cache VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                "encrypted=excluded.encrypted,expires=excluded.expires,created=excluded.created",
                (key, self.cipher.encrypt(body), expires, created),
            )

    def purge(self) -> int:
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            return db.execute(
                "DELETE FROM response_cache WHERE expires<=?", (self._now().timestamp(),)
            ).rowcount


class GovernedModelExecutor:
    def __init__(
        self,
        *,
        routes: tuple[ModelRoute, ...],
        registry: CapabilityRegistry,
        budgets: TaskBudgetLedger,
        providers: Mapping[tuple[str, str], Callable[[RoutingRequest], Awaitable[ProviderResult]]],
        cache: ScopedResponseCache | None = None,
        authorized: Callable[[ActorContext, RoutingRequest], bool],
        enabled: Callable[[], bool] = lambda: True,
    ):
        if len({(r.name, r.version) for r in routes}) != len(routes):
            raise ValueError("duplicate_model_route")
        self.routes, self.registry, self.budgets = routes, registry, budgets
        if any(not inspect.iscoroutinefunction(provider) for provider in providers.values()):
            raise ValueError("model providers must implement an async bounded adapter")
        self.providers, self.cache = dict(providers), cache
        self.authorized, self.enabled = authorized, enabled

    def execute(self, task_id: UUID, actor: ActorContext, request: RoutingRequest) -> dict:
        return asyncio.run(self.aexecute(task_id, actor, request))

    async def aexecute(self, task_id: UUID, actor: ActorContext, request: RoutingRequest) -> dict:
        request = RoutingRequest.model_validate_json(request.model_dump_json())
        if self.enabled() is not True or self.authorized(actor, request) is not True:
            raise PermissionError("model_access_denied")
        TextDLPScanner().check(request.model_dump_json().encode())
        self.budgets.ensure_active(task_id, actor)
        candidates = []
        for route in self.routes:
            if (
                route.enabled is not True
                or request.purpose not in route.purposes
                or request.classification not in route.classifications
                or route.cost_bound(request) > request.max_cost_microunits
                or route.estimated_latency_ms > request.max_latency_ms
                or (route.name, route.version) not in self.providers
            ):
                continue
            try:
                capability = self.registry.require(
                    route.name, route.version, route.content_hash, actor
                )
            except PermissionError:
                continue
            if capability.kind == "model":
                candidates.append(route)
        if not candidates:
            raise PermissionError("no_admitted_model_within_budget")
        route = min(
            candidates, key=lambda r: (r.cost_bound(request), r.estimated_latency_ms, r.name)
        )
        started = monotonic()
        cache_key = ScopedResponseCache.cache_key(request, actor, route)
        cached = self.cache.get(cache_key) if self.cache is not None else None
        if cached is not None:
            # Cache access remains subject to current authorization, admission and deadline.
            self._recheck(task_id, actor, request, route)
            return self._result(cached, route, True, 0, started)
        cost = route.cost_bound(request)
        self.budgets.reserve(
            task_id,
            actor,
            tokens=request.input_token_bound + request.output_token_bound,
            cost_microunits=cost,
        )
        self._recheck(task_id, actor, request, route)
        async with asyncio.timeout(request.max_latency_ms / 1000):
            result = ProviderResult.model_validate(
                await self.providers[(route.name, route.version)](request)
            )
        if (
            result.input_tokens > request.input_token_bound
            or result.output_tokens > request.output_token_bound
        ):
            raise PermissionError("provider_token_bound_exceeded")
        TextDLPScanner().check(result.model_dump_json().encode())
        self._recheck(task_id, actor, request, route)
        if (monotonic() - started) * 1000 > request.max_latency_ms:
            raise TimeoutError("model_latency_budget_exceeded")
        if self.cache is not None:
            self.cache.put(cache_key, result)
        return self._result(result, route, False, cost, started)

    def _recheck(self, task_id, actor, request, route):
        if route not in self.routes or route.enabled is not True:
            raise PermissionError("model_route_disabled_or_changed")
        if self.enabled() is not True or self.authorized(actor, request) is not True:
            raise PermissionError("model_access_denied")
        self.registry.require(route.name, route.version, route.content_hash, actor)
        self.budgets.ensure_active(task_id, actor)

    @staticmethod
    def _result(result, route, cached, reserved_cost, started):
        return {
            "result": result.model_dump(mode="json"),
            "model": route.name,
            "version": route.version,
            "cached": cached,
            "reserved_cost_microunits": reserved_cost,
            "duration_ms": (monotonic() - started) * 1000,
        }
