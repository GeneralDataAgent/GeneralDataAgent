"""Runtime selectors remain pinned, secretless and independent of desktop configuration."""

import pytest
from pydantic import SecretStr

from generaldataagent.evaluation.stage_six_models import resolve_evaluation_model
from generaldataagent.llm import OpenAiCcSwitchProvider


@pytest.fixture
def provider(monkeypatch):
    value = OpenAiCcSwitchProvider(
        provider_id="p",
        provider_name="test",
        app_type="codex",
        base_url="https://example.test/v1",
        model="old/model",
        wire_api="responses",
        api_key=SecretStr("NEVER_EXPOSE_THIS"),
    )
    monkeypatch.setattr(OpenAiCcSwitchProvider, "load", lambda **kwargs: value)
    return value


def test_model_override_is_memory_only_and_route_excludes_credentials(provider):
    selected, route = resolve_evaluation_model(
        app_type="codex",
        provider_id="p",
        model_override="new/pinned-model",
        transport="openai-chat",
    )
    assert provider.model == "old/model"
    assert selected.model == "new/pinned-model"
    assert route["wire_api"] == "chat_completions"
    assert "NEVER_EXPOSE_THIS" not in str(route)
    assert "example.test" not in str(route)
    assert len(route["route_hash"]) == 64


@pytest.mark.parametrize("model", ["~floating-latest", "bad model", "x\nsecret"])
def test_unsafe_or_floating_selectors_rejected(provider, model):
    with pytest.raises(ValueError):
        resolve_evaluation_model(app_type="codex", provider_id="p", model_override=model)


def test_route_fingerprint_detects_target_drift(provider):
    _, a = resolve_evaluation_model(app_type="codex", provider_id="p")
    _, b = resolve_evaluation_model(app_type="codex", provider_id="p", model_override="other/model")
    assert a["route_hash"] != b["route_hash"]


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://user:password@example.test/v1",
        "https://example.test/v1?key=hidden",
        "https://example.test/v1#secret",
    ],
)
def test_secret_bearing_endpoints_rejected(provider, monkeypatch, endpoint):
    changed = provider.model_copy(update={"base_url": endpoint})
    monkeypatch.setattr(OpenAiCcSwitchProvider, "load", lambda **kwargs: changed)
    with pytest.raises(ValueError):
        resolve_evaluation_model(app_type="codex", provider_id="p")


def test_native_thinking_and_json_parameters_are_explicit(provider, monkeypatch):
    import generaldataagent.evaluation.stage_six_models as module

    selected, route = resolve_evaluation_model(
        app_type="codex",
        provider_id="p",
        model_override="nvidia/nemotron-3.5-lightning-30b-a3b",
        transport="openai-chat",
        thinking_mode="off",
        json_mode=True,
    )
    captured = {}

    def build(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(module, "ChatOpenAI", build)
    module.build_evaluation_model(selected, route)
    assert captured["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert captured["model_kwargs"] == {"response_format": {"type": "json_object"}}
    with pytest.raises(ValueError):
        resolve_evaluation_model(app_type="codex", provider_id="p", thinking_mode="off")


def test_output_cap_is_request_scoped_and_fingerprinted(provider, monkeypatch):
    import generaldataagent.evaluation.stage_six_models as module

    selected, limited = resolve_evaluation_model(
        app_type="codex",
        provider_id="p",
        transport="openai-chat",
        max_output_tokens=1024,
    )
    _, default = resolve_evaluation_model(
        app_type="codex", provider_id="p", transport="openai-chat"
    )
    assert limited["route_hash"] != default["route_hash"]
    captured = {}
    monkeypatch.setattr(module, "ChatOpenAI", lambda **kwargs: captured.update(kwargs))
    module.build_evaluation_model(selected, limited)
    assert captured["max_tokens"] == 1024
    assert provider.model == "old/model"


@pytest.mark.parametrize("cap", [0, 31, 3001, True, 1.5])
def test_invalid_output_caps_are_rejected(provider, cap):
    with pytest.raises(ValueError, match="output token limit"):
        resolve_evaluation_model(app_type="codex", provider_id="p", max_output_tokens=cap)
