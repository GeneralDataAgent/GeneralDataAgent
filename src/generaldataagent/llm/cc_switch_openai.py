"""Load an OpenAI-compatible Responses model from CC Switch without copying credentials."""

import json
import sqlite3
import tomllib
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from generaldataagent.llm.cc_switch import LlmConfigurationError


class OpenAiCcSwitchProvider(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_id: str
    provider_name: str
    app_type: Literal["codex"]
    base_url: str
    model: str
    wire_api: str
    api_key: SecretStr = Field(repr=False)

    @classmethod
    def load(
        cls,
        database_path: Path | str = Path("~/.cc-switch/cc-switch.db"),
        *,
        provider_id: str | None = None,
        provider_name: str | None = None,
    ) -> "OpenAiCcSwitchProvider":
        if provider_id and provider_name:
            raise ValueError("select a CC Switch provider by id or name, not both")
        path = Path(database_path).expanduser().resolve(strict=True)
        where = "app_type = ? AND is_current = 1"
        parameters: tuple[str, ...] = ("codex",)
        if provider_id:
            where = "app_type = ? AND id = ?"
            parameters = ("codex", provider_id)
        elif provider_name:
            where = "app_type = ? AND name = ?"
            parameters = ("codex", provider_name)
        uri = f"file:{path.as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            rows = connection.execute(
                f"SELECT id, name, settings_config FROM providers WHERE {where}", parameters
            ).fetchall()
        if len(rows) != 1:
            selector = provider_id or provider_name or "current provider"
            raise LlmConfigurationError(
                f"expected one CC Switch codex provider for {selector!r}, found {len(rows)}"
            )
        resolved_id, name, raw_settings = rows[0]
        try:
            settings = json.loads(raw_settings)
            config = tomllib.loads(settings["config"])
            provider_config = config["model_providers"][config["model_provider"]]
            return cls(
                provider_id=resolved_id,
                provider_name=name,
                app_type="codex",
                base_url=_openai_sdk_base_url(provider_config["base_url"]),
                model=config["model"],
                wire_api=provider_config.get("wire_api", "chat_completions"),
                api_key=SecretStr(settings["auth"]["OPENAI_API_KEY"]),
            )
        except (KeyError, TypeError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
            raise LlmConfigurationError(
                f"CC Switch provider {name!r} is not OpenAI-compatible"
            ) from exc

    def build_chat_model(
        self,
        *,
        timeout_seconds: float = 120,
        max_retries: int = 1,
    ) -> ChatOpenAI:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        return ChatOpenAI(
            model=self.model,
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=timeout_seconds,
            temperature=0,
            max_retries=max_retries,
            use_responses_api=self.wire_api == "responses",
        )


def _openai_sdk_base_url(value: str) -> str:
    normalized = value.rstrip("/")
    parts = urlsplit(normalized)
    if parts.hostname == "openrouter.ai" and parts.path == "/api":
        return urlunsplit((parts.scheme, parts.netloc, "/api/v1", parts.query, parts.fragment))
    return normalized
