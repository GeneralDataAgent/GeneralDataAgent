import json
import sqlite3

from pydantic import SecretStr

from generaldataagent.llm import OpenAiCcSwitchProvider
from generaldataagent.llm.cc_switch_openai import _openai_sdk_base_url


def create_provider_database(path) -> None:
    settings = {
        "auth": {"OPENAI_API_KEY": "secret-value"},
        "config": """
model_provider = "custom"
model = "test-model"

[model_providers.custom]
name = "Test"
base_url = "https://models.example.test/v1/"
wire_api = "responses"
requires_openai_auth = true
""",
    }
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE providers (
                id TEXT,
                name TEXT,
                app_type TEXT,
                settings_config TEXT,
                is_current INTEGER
            )
            """
        )
        connection.execute(
            "INSERT INTO providers VALUES (?, ?, ?, ?, ?)",
            ("provider-id", "Test Provider", "codex", json.dumps(settings), 1),
        )


def test_loads_openai_compatible_provider_without_exposing_secret(tmp_path) -> None:
    database = tmp_path / "cc-switch.db"
    create_provider_database(database)

    provider = OpenAiCcSwitchProvider.load(database, provider_id="provider-id")

    assert provider.provider_name == "Test Provider"
    assert provider.model == "test-model"
    assert provider.base_url == "https://models.example.test/v1"
    assert provider.wire_api == "responses"
    assert provider.api_key == SecretStr("secret-value")
    assert "secret-value" not in repr(provider)


def test_normalizes_openrouter_responses_base_url() -> None:
    assert _openai_sdk_base_url("https://openrouter.ai/api") == "https://openrouter.ai/api/v1"
    assert _openai_sdk_base_url("https://integrate.api.nvidia.com/v1") == (
        "https://integrate.api.nvidia.com/v1"
    )
