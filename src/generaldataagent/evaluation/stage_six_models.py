"""Per-run model selection without changing CC Switch or copying credentials."""

import hashlib
import json
from urllib.parse import urlsplit

from langchain_openai import ChatOpenAI

from generaldataagent.llm import CcSwitchProvider, OpenAiCcSwitchProvider


def resolve_evaluation_model(
    *,
    app_type: str,
    provider_id: str | None,
    model_override: str | None = None,
    transport: str = "configured",
    thinking_mode: str = "default",
    json_mode: bool = False,
):
    if app_type not in {"claude", "codex"} or transport not in {"configured", "openai-chat"}:
        raise ValueError("unsupported evaluation connection")
    provider = (CcSwitchProvider if app_type == "claude" else OpenAiCcSwitchProvider).load(
        provider_id=provider_id
    )
    model = model_override or provider.model
    if not model or len(model) > 200 or any(c.isspace() for c in model) or model.startswith("~"):
        raise ValueError("invalid or floating model selector")
    if thinking_mode not in {"default", "on", "off"} or type(json_mode) is not bool:
        raise ValueError("invalid reasoning or JSON setting")
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
        "endpoint_hash": hashlib.sha256(base.encode()).hexdigest(),
        "wire_api": wire,
    }
    route["route_hash"] = hashlib.sha256(json.dumps(route, sort_keys=True).encode()).hexdigest()
    return provider, route


def build_evaluation_model(provider, route: dict):
    if route["transport"] == "openai-chat":
        base = provider.base_url.rstrip("/")
        if not base.endswith("/v1"):
            base += "/v1"
        options = {}
        if route.get("thinking_mode", "default") != "default":
            options["extra_body"] = {
                "chat_template_kwargs": {"enable_thinking": route["thinking_mode"] == "on"}
            }
        if route.get("json_mode"):
            options["model_kwargs"] = {"response_format": {"type": "json_object"}}
        return ChatOpenAI(
            model=provider.model,
            api_key=provider.api_key,
            base_url=base,
            timeout=30,
            max_retries=0,
            temperature=0,
            max_tokens=3000,
            **options,
        )
    return provider.build_chat_model(timeout_seconds=30, max_retries=0).bind(max_tokens=3000)
