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
