"""Metered narration attached to the certified metric workflow.

The fresh deterministic query result is always computed before cache lookup. Cache reuse
never replaces SQL authorization or query execution. Versions come from trusted config.
"""

import json
from collections.abc import Callable, Mapping
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from generaldataagent.business.optimization import (
    GovernedModelExecutor,
    ProviderResult,
    RoutingRequest,
)
from generaldataagent.contracts import ActorContext
from generaldataagent.contracts.models import ContractModel
from generaldataagent.llm.cc_switch import LangChainNarrator, LlmInvocationError, Narrative


class NarrationSettings(ContractModel):
    prompt_version: str = Field(min_length=1, max_length=128)
    policy_version: str = Field(min_length=1, max_length=128)
    output_token_bound: int = Field(default=1024, ge=1, le=10000)
    max_cost_microunits: int = Field(default=100000, ge=0)
    max_latency_ms: int = Field(default=15000, ge=1, le=60000)
    classification: Literal["public", "internal"] = "internal"


class RoutedNarrator:
    def __init__(
        self,
        *,
        executor: GovernedModelExecutor,
        settings: NarrationSettings,
        authorization_version: Callable[[ActorContext], str],
    ):
        self.executor, self.settings = executor, settings
        self.authorization_version = authorization_version

    def generate(
        self, evidence: Mapping[str, Any], *, task_id: UUID, actor: ActorContext, source_hash: str
    ) -> tuple[Narrative, dict]:
        settings = self.settings
        prompt = json.dumps(evidence, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        # Reserve a conservative byte-based envelope, including system text and tool schema.
        input_bound = (
            len(prompt.encode())
            + len(LangChainNarrator.SYSTEM_PROMPT.encode())
            + len(json.dumps(Narrative.model_json_schema()).encode())
            + 4096
        )
        request = RoutingRequest(
            purpose="narrative",
            prompt=prompt,
            source_hash=source_hash,
            prompt_version=settings.prompt_version,
            policy_version=settings.policy_version,
            authorization_version=self.authorization_version(actor),
            classification=settings.classification,
            input_token_bound=input_bound,
            output_token_bound=settings.output_token_bound,
            max_cost_microunits=settings.max_cost_microunits,
            max_latency_ms=settings.max_latency_ms,
        )
        result = self.executor.execute(task_id, actor, request)
        try:
            narrative = Narrative.model_validate_json(result["result"]["text"])
        except ValueError as exc:
            raise LlmInvocationError("routed narrator returned invalid structured prose") from exc
        narrative = narrative.model_copy(update={"model": f"{result['model']}@{result['version']}"})
        usage = {
            key: result[key]
            for key in ("model", "version", "cached", "reserved_cost_microunits", "duration_ms")
        }
        usage.update(
            {
                key: 0 if result["cached"] else result["result"][key]
                for key in ("input_tokens", "output_tokens")
            }
        )
        return narrative, usage


class LangChainRoutedProvider:
    """Async bridge for an operator-selected model, including mandatory provider usage.

    Do not infer token counts when the provider omits them. Provider transport timeouts
    and retry configuration must also be set when constructing the underlying model.
    """

    def __init__(
        self,
        model: Any,
        *,
        output_token_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens",
    ):
        if output_token_parameter not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError("unsupported output-token parameter")
        self.structured = model.with_structured_output(Narrative, include_raw=True)
        self.output_token_parameter = output_token_parameter

    async def invoke(self, request: RoutingRequest) -> ProviderResult:
        try:
            response = await self.structured.ainvoke(
                [("system", LangChainNarrator.SYSTEM_PROMPT), ("human", request.prompt)],
                **{self.output_token_parameter: request.output_token_bound},
            )
            if response.get("parsing_error") is not None:
                raise ValueError("structured response parsing failed")
            narrative = Narrative.model_validate(response["parsed"])
            usage = response["raw"].usage_metadata
            if not isinstance(usage, dict) or any(
                type(usage.get(k)) is not int or usage[k] < 0
                for k in ("input_tokens", "output_tokens")
            ):
                raise ValueError("provider token usage is required")
            return ProviderResult(
                text=narrative.model_dump_json(),
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
            )
        except Exception as exc:
            raise LlmInvocationError(
                f"routed model invocation failed: {type(exc).__name__}"
            ) from exc
