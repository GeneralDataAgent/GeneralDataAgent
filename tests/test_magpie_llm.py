"""Magpie discovery is read-only and never puts gateway credentials in evidence."""

import pytest

from generaldataagent.llm import LlmConfigurationError, MagpieProvider


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("""model = "unrelated/global-model"
[model_providers.magpie]
base_url = "http://127.0.0.1:3425/v1"
wire_api = "responses"
experimental_bearer_token = "PRIVATE_GATEWAY_KEY"
[model_providers.magpie.http_headers]
x-openai-actor-authorization = "PRIVATE_ACTOR_KEY"
""")
    return path


def test_load_is_read_only_and_model_must_be_explicit(config):
    before = config.read_bytes()
    with pytest.raises(LlmConfigurationError, match="explicit model"):
        MagpieProvider.load(config)
    p = MagpieProvider.load(config, model="workbuddy/deepseek-v4.1-flash")
    assert p.model == "workbuddy/deepseek-v4.1-flash"
    assert p.wire_api == "responses"
    assert p.provider_id == "magpie"
    assert config.read_bytes() == before
    assert "PRIVATE" not in repr(p)
    assert "PRIVATE" not in p.model_dump_json()


@pytest.mark.parametrize(
    "change",
    [
        ("http://127.0.0.1:3425/v1", "http://user:PRIVATE_GATEWAY_KEY@localhost/v1"),
        ("http://127.0.0.1:3425/v1", "http://localhost/v1?key=PRIVATE_GATEWAY_KEY"),
        ('"responses"', '"invalid"'),
        ('"PRIVATE_GATEWAY_KEY"', '""'),
        ("x-openai-actor-authorization", "authorization"),
        ('"PRIVATE_ACTOR_KEY"', "42"),
        ('"PRIVATE_ACTOR_KEY"', '"bad\\nheader"'),
    ],
)
def test_invalid_config_diagnostics_never_echo_secrets(config, change):
    config.write_text(config.read_text().replace(*change))
    with pytest.raises(LlmConfigurationError) as e:
        MagpieProvider.load(config, model="pinned/model")
    assert "PRIVATE" not in str(e.value)
    assert e.value.__suppress_context__


def test_missing_selector_has_safe_diagnostic(config):
    with pytest.raises(LlmConfigurationError, match="unavailable or invalid"):
        MagpieProvider.load(config, provider_id="missing", model="pinned/model")


def test_responses_and_chat_preserve_gateway_headers_and_cap(config, monkeypatch):
    import generaldataagent.evaluation.stage_six_models as evaluation
    import generaldataagent.llm.magpie as gateway

    p = MagpieProvider.load(config, model="workbuddy/deepseek-v4.1-flash")
    captured = {}

    class Model:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def bind(self, **kwargs):
            captured.update(kwargs)
            return self

    monkeypatch.setattr(gateway, "ChatOpenAI", Model)
    monkeypatch.setattr(evaluation, "ChatOpenAI", Model)
    monkeypatch.setattr(MagpieProvider, "load", lambda **kwargs: p)
    _, route = evaluation.resolve_evaluation_model(
        app_type="magpie", provider_id="magpie", model_override=p.model, max_output_tokens=1024
    )
    evaluation.build_evaluation_model(p, route)
    assert captured["use_responses_api"] is True
    assert captured["max_tokens"] == 1024
    assert captured["default_headers"]["x-openai-actor-authorization"] == "PRIVATE_ACTOR_KEY"
    assert "PRIVATE" not in str(route)
    assert "gateway_headers_hash" in route
    captured.clear()
    _, chat = evaluation.resolve_evaluation_model(
        app_type="magpie",
        provider_id="magpie",
        model_override=p.model,
        transport="openai-chat",
        json_mode=True,
    )
    evaluation.build_evaluation_model(p, chat)
    assert captured["base_url"] == "http://127.0.0.1:3425/v1"
    assert captured["model_kwargs"]["response_format"] == {"type": "json_object"}
    assert captured["default_headers"] == p.request_headers()
    assert chat["route_hash"] != route["route_hash"]


def test_header_changes_are_detected_between_parent_and_worker(config, monkeypatch):
    from generaldataagent.evaluation.stage_six_models import resolve_evaluation_model

    p = MagpieProvider.load(config, model="pinned/model")
    monkeypatch.setattr(MagpieProvider, "load", lambda **kwargs: p)
    _, first = resolve_evaluation_model(
        app_type="magpie", provider_id="magpie", model_override=p.model
    )
    config.write_text(config.read_text().replace("PRIVATE_ACTOR_KEY", "ROTATED_ACTOR_KEY"))
    p = p.model_copy(update={"headers": {}})
    _, changed = resolve_evaluation_model(
        app_type="magpie", provider_id="magpie", model_override=p.model
    )
    assert first["route_hash"] != changed["route_hash"]
