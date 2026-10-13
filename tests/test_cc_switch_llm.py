import json
import sqlite3

import pytest
from pydantic import SecretStr

from generaldataagent.llm import CcSwitchProvider, LlmConfigurationError


def create_provider_database(path, *, current: bool = True) -> None:
    settings = {
        "env": {
            "ANTHROPIC_BASE_URL": "https://models.example.test/v1",
            "ANTHROPIC_MODEL": "test-model",
            "ANTHROPIC_AUTH_TOKEN": "secret-value",
        }
    }
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE providers (
                name TEXT,
                app_type TEXT,
                settings_config TEXT,
                is_current INTEGER
            )
            """
        )
        connection.execute(
            "INSERT INTO providers VALUES (?, ?, ?, ?)",
            ("Test Provider", "claude", json.dumps(settings), int(current)),
        )


def test_loads_current_provider_without_exposing_secret(tmp_path) -> None:
    database = tmp_path / "cc-switch.db"
    create_provider_database(database)
    provider = CcSwitchProvider.load(database)

    assert provider.provider_name == "Test Provider"
    assert provider.model == "test-model"
    assert provider.base_url == "https://models.example.test"
    assert provider.api_key == SecretStr("secret-value")
    assert "secret-value" not in repr(provider)


def test_rejects_missing_current_provider(tmp_path) -> None:
    database = tmp_path / "cc-switch.db"
    create_provider_database(database, current=False)
    with pytest.raises(LlmConfigurationError, match="found 0"):
        CcSwitchProvider.load(database)
