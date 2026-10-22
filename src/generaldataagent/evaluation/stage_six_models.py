"""Per-run model selection without changing CC Switch or copying credentials."""

import hashlib
import inspect
import json
from urllib.parse import urlsplit

from langchain_openai import ChatOpenAI

from generaldataagent.llm import CcSwitchProvider, MagpieProvider, OpenAiCcSwitchProvider


def resolve_evaluation_model(
    *,
    app_type: str,
    provider_id: str | None,
    model_override: str | None = None,
    transport: str = "configured",
    thinking_mode: str = "default",
    json_mode: bool = False,
    max_output_tokens: int | None = None,
):
    if app_type not in {"claude", "codex", "magpie"} or transport not in {
        "configured",
        "openai-chat",
    }:
        raise ValueError("unsupported evaluation connection")
    if app_type == "magpie":
        provider = MagpieProvider.load(provider_id=provider_id, model=model_override)
    else:
        provider = (CcSwitchProvider if app_type == "claude" else OpenAiCcSwitchProvider).load(
            provider_id=provider_id
        )
    model = model_override or provider.model
    if not model or len(model) > 200 or any(c.isspace() for c in model) or model.startswith("~"):
        raise ValueError("invalid or floating model selector")
    if thinking_mode not in {"default", "on", "off"} or type(json_mode) is not bool:
        raise ValueError("invalid reasoning or JSON setting")
    if max_output_tokens is not None and (
        type(max_output_tokens) is not int or max_output_tokens < 32
    ):
        raise ValueError("output token limit must be at least 32 or omitted")
    if json_mode and transport != "openai-chat":
        raise ValueError("JSON mode is qualified only for OpenAI chat transport")
    if thinking_mode != "default" and (
        transport != "openai-chat" or not model.startswith("nvidia/nemotron")
    ):
        raise ValueError("thinking toggle is qualified only for native Nemotron chat")
    provider = provider.model_copy(update={"model": model})
    base = provider.base_url.rstrip("/")
    parts = urlsplit(base)
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("evaluation endpoint must not contain embedded credentials or parameters")
    if transport == "openai-chat" and not base.endswith("/v1"):
        base += "/v1"
    wire = (
        "chat_completions"
        if transport == "openai-chat"
        else getattr(provider, "wire_api", "anthropic")
    )
    route = {
        "provider_id": provider.provider_id,
        "app_type": app_type,
        "model": model,
        "transport": transport,
        "thinking_mode": thinking_mode,
        "json_mode": json_mode,
        "max_output_tokens": max_output_tokens,
        "endpoint_hash": hashlib.sha256(base.encode()).hexdigest(),
        "wire_api": wire,
    }
    if isinstance(provider, MagpieProvider):
        route["gateway_adapter_hash"] = hashlib.sha256(
            inspect.getsource(MagpieProvider).encode()
        ).hexdigest()
        route["gateway_headers_hash"] = hashlib.sha256(
            json.dumps(provider.request_headers(), sort_keys=True).encode()
        ).hexdigest()
    route["route_hash"] = hashlib.sha256(json.dumps(route, sort_keys=True).encode()).hexdigest()
    return provider, route


def build_evaluation_model(provider, route: dict):
    if route["transport"] == "openai-chat":
        base = provider.base_url.rstrip("/")
        if not base.endswith("/v1"):
            base += "/v1"
        options = {}
        if isinstance(provider, MagpieProvider):
            options["default_headers"] = provider.request_headers()
        if route.get("thinking_mode", "default") != "default":
            options["extra_body"] = {
                "chat_template_kwargs": {"enable_thinking": route["thinking_mode"] == "on"}
            }
        if route.get("json_mode"):
            options["model_kwargs"] = {"response_format": {"type": "json_object"}}
        if route.get("max_output_tokens") is not None:
            options["max_tokens"] = route["max_output_tokens"]
        return ChatOpenAI(
            model=provider.model,
            api_key=provider.api_key,
            base_url=base,
            timeout=30,
            max_retries=0,
            temperature=0,
            **options,
        )
    model = provider.build_chat_model(timeout_seconds=30, max_retries=0)
    return (
        model.bind(max_tokens=route["max_output_tokens"])
        if route.get("max_output_tokens") is not None
        else model
    )
