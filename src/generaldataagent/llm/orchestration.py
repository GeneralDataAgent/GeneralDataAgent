"""Per-call admitted native-model routing shared by Supervisor and specialists."""

import asyncio
import json
from collections.abc import Callable, Mapping
from time import monotonic
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage
from langchain_core.utils.function_calling import convert_to_json_schema, convert_to_openai_tool
from langchain_openai import ChatOpenAI
from pydantic import Field, field_validator

from generaldataagent.business.actions import content_hash
from generaldataagent.business.governance import CapabilityRegistry
from generaldataagent.business.optimization import ModelRoute, RoutingRequest
from generaldataagent.contracts import ActorContext, AuditEvent, Budget
from generaldataagent.contracts.models import ContractModel
from generaldataagent.llm.routed import NarrationSettings
from generaldataagent.policy.budgets import TaskBudgetLedger
from generaldataagent.security.dlp import TextDLPScanner


class OrchestrationModelConfig(ContractModel):
    """Non-secret native client configuration to enroll before use.

    SDK retries and streaming are disabled. Credentials come from the provider's
    protected environment; an optional endpoint is part of the reviewed fingerprint.
    """

    provider: Literal["anthropic", "openai"]
    model_name: str = Field(min_length=1, max_length=128)
    base_url: str | None = Field(default=None, pattern=r"^https://[^\s]+$")
    timeout_seconds: int = Field(default=30, ge=1, le=60)

    @field_validator("base_url")
    @classmethod
    def validate_endpoint(cls, value):
        if value is not None:
            parsed = urlsplit(value)
            if (
                not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("model_endpoint_requires_https_without_credentials_or_query")
            _ = parsed.port
        return value

    @property
    def effective_base_url(self) -> str:
        return self.base_url or (
            "https://api.anthropic.com"
            if self.provider == "anthropic"
            else "https://api.openai.com/v1"
        )

    @property
    def fingerprint(self) -> str:
        return content_hash(
            {
                "configuration": self.model_dump(mode="json"),
                "effective_base_url": self.effective_base_url,
                "adapter_version": "native-chat-v1",
                "max_retries": 0,
                "streaming": False,
                "cache": False,
            }
        )

    def build(self):
        options = dict(max_retries=0, timeout=self.timeout_seconds, streaming=False, cache=False)
        options["base_url"] = self.effective_base_url
        if self.provider == "anthropic":
            return ChatAnthropic(model_name=self.model_name, **options)
        return ChatOpenAI(model=self.model_name, **options)


class OrchestrationRouter:
    def __init__(
        self,
        *,
        routes: tuple[ModelRoute, ...],
        configs: Mapping[tuple[str, str], OrchestrationModelConfig],
        registry: CapabilityRegistry,
        budgets: TaskBudgetLedger,
        audit: Any,
        settings: NarrationSettings,
        authorized: Callable[[ActorContext, RoutingRequest], bool],
        authorization_version: Callable[[ActorContext], str],
        budget: Budget,
        max_task_cost_microunits: int = 1000000,
        enabled: Callable[[], bool] = lambda: False,
    ):
        if not routes or len({(r.name, r.version) for r in routes}) != len(routes):
            raise ValueError("orchestration_requires_unique_routes")
        self.routes, self.configs = routes, dict(configs)
        self.registry, self.budgets, self.audit = registry, budgets, audit
        self.settings, self.authorized = settings, authorized
        self.authorization_version, self.enabled = authorization_version, enabled
        self.budget, self.max_task_cost = budget, max_task_cost_microunits
        if audit is None:
            raise ValueError("orchestration_requires_usage_audit")
        if len({config.provider for config in self.configs.values()}) != 1:
            raise ValueError("orchestration_routes_require_one_native_provider_family")
        self.models = {}
        for route in routes:
            config = self.configs.get((route.name, route.version))
            if config is None or config.fingerprint != route.content_hash:
                raise ValueError("orchestration_model_configuration_mismatch")
            self.models[(route.name, route.version)] = config.build()

    @property
    def bootstrap_model(self):
        # Native model objects preserve Deep Agents' provider-specific tool exclusions.
        return next(iter(self.models.values()))

    def for_task(self, actor: ActorContext, *, thread_id: str, source_hash: str):
        task_id = uuid5(
            NAMESPACE_URL,
            "gda:orchestration:"
            + content_hash(
                {
                    "actor": json.loads(TaskBudgetLedger._scope(actor)),
                    "thread_id": thread_id,
                }
            ),
        )
        self.budgets.register(task_id, actor, self.budget, max_cost_microunits=self.max_task_cost)
        return OrchestrationMiddleware(self, task_id, actor, source_hash)

    def _check(self, route, actor, request):
        if self.enabled() is not True or self.authorized(actor, request) is not True:
            raise PermissionError("orchestration_access_denied")
        config = self.configs[(route.name, route.version)]
        if (
            route not in self.routes
            or not route.enabled
            or config.fingerprint != route.content_hash
        ):
            raise PermissionError("orchestration_route_changed_or_disabled")
        capability = self.registry.require(route.name, route.version, route.content_hash, actor)
        if capability.kind != "model":
            raise PermissionError("orchestration_requires_model_admission")


class OrchestrationMiddleware(AgentMiddleware):
    def __init__(self, router: OrchestrationRouter, task_id: UUID, actor, source_hash):
        self.router, self.task_id = router, task_id
        self.actor, self.source_hash = actor, source_hash

    async def awrap_model_call(self, request: ModelRequest, handler):
        router, settings = self.router, self.router.settings
        schema = getattr(request.response_format, "schema", request.response_format)
        for message in request.messages + (
            [request.system_message] if request.system_message else []
        ):
            if not isinstance(message.content, str) and (
                not isinstance(message.content, list)
                or any(
                    not isinstance(block, dict) or block.get("type") not in {"text", "tool_use"}
                    for block in message.content
                )
            ):
                raise PermissionError("orchestration_requires_bounded_text_content")
        envelope = {
            "messages": [m.model_dump(mode="json") for m in request.messages],
            "system": request.system_message.model_dump(mode="json")
            if request.system_message
            else None,
            "tools": [convert_to_openai_tool(t) for t in request.tools],
            "response_format": convert_to_json_schema(schema) if schema is not None else None,
            "tool_choice": request.tool_choice,
        }
        raw = json.dumps(envelope, ensure_ascii=False, sort_keys=True).encode()
        TextDLPScanner(max_bytes=95000).check(raw)
        routing = RoutingRequest(
            purpose="orchestration",
            prompt=content_hash(envelope),
            source_hash=self.source_hash,
            prompt_version=settings.prompt_version,
            policy_version=settings.policy_version,
            authorization_version=router.authorization_version(self.actor),
            classification=settings.classification,
            input_token_bound=len(raw) + 4096,
            output_token_bound=settings.output_token_bound,
            max_cost_microunits=settings.max_cost_microunits,
            max_latency_ms=settings.max_latency_ms,
        )
        router.budgets.ensure_active(self.task_id, self.actor)
        candidates = []
        for route in router.routes:
            if (
                routing.purpose not in route.purposes
                or routing.classification not in route.classifications
                or route.cost_bound(routing) > routing.max_cost_microunits
                or route.estimated_latency_ms > routing.max_latency_ms
            ):
                continue
            try:
                router._check(route, self.actor, routing)
            except PermissionError:
                continue
            candidates.append(route)
        if not candidates:
            raise PermissionError("no_admitted_orchestration_route")
        route = min(
            candidates, key=lambda r: (r.cost_bound(routing), r.estimated_latency_ms, r.name)
        )
        cost = route.cost_bound(routing)
        router.budgets.reserve(
            self.task_id,
            self.actor,
            tokens=routing.input_token_bound + routing.output_token_bound,
            cost_microunits=cost,
        )
        config = router.configs[(route.name, route.version)]
        # Fresh native clients keep configuration immutable across concurrent tasks/calls.
        model = config.build()
        token_parameter = (
            "max_tokens" if config.provider == "anthropic" else "max_completion_tokens"
        )
        router._check(route, self.actor, routing)
        if router.authorization_version(self.actor) != routing.authorization_version:
            raise PermissionError("orchestration_authorization_changed")
        self._emit("orchestration.model_reserved", route, {"cost_microunits": cost})
        started = monotonic()
        async with asyncio.timeout(routing.max_latency_ms / 1000):
            response = await handler(
                request.override(
                    model=model,
                    model_settings={token_parameter: routing.output_token_bound},
                )
            )
        if len(response.result) != 1 or not isinstance(response.result[0], AIMessage):
            raise PermissionError("orchestration_requires_single_provider_message")
        message = response.result[0]
        usage = message.usage_metadata or {}
        if any(
            type(usage.get(k)) is not int or usage[k] < 0 for k in ("input_tokens", "output_tokens")
        ):
            raise PermissionError("orchestration_requires_provider_usage")
        if (
            usage["input_tokens"] > routing.input_token_bound
            or usage["output_tokens"] > routing.output_token_bound
        ):
            raise PermissionError("orchestration_provider_token_bound_exceeded")
        TextDLPScanner().check(message.model_dump_json().encode())
        router._check(route, self.actor, routing)
        if router.authorization_version(self.actor) != routing.authorization_version:
            raise PermissionError("orchestration_authorization_changed")
        router.budgets.ensure_active(self.task_id, self.actor)
        self._emit(
            "orchestration.model_usage",
            route,
            {
                "duration_ms": int((monotonic() - started) * 1000),
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
                "cost_microunits": usage["input_tokens"] * route.input_cost_per_token
                + usage["output_tokens"] * route.output_cost_per_token,
            },
        )
        return response

    def _emit(self, event_type, route, payload):
        self.router.audit.emit(
            AuditEvent(
                event_type=event_type,
                task_id=self.task_id,
                actor_id=self.actor.user_id,
                tenant_id=self.actor.tenant_id,
                trace_id=str(self.task_id),
                payload={"model": route.name, "version": route.version, **payload},
            )
        )
