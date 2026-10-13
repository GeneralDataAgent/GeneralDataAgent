"""Load a model from CC Switch without copying credentials into the project."""

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from langchain_anthropic import ChatAnthropic
from pydantic import BaseModel, ConfigDict, Field, SecretStr


class LlmConfigurationError(RuntimeError):
    """Raised when a selected local model configuration is unavailable."""


class LlmInvocationError(RuntimeError):
    """Raised when a model result violates the narrative contract."""


class Narrative(BaseModel):
    """Small, evidence-bounded narrative returned by the model."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=1_000)
    insights: tuple[str, ...] = Field(default=(), max_length=5)
    limitations: tuple[str, ...] = Field(default=(), max_length=5)
    model: str | None = None


class CcSwitchProvider(BaseModel):
    """A resolved provider whose secret remains wrapped and excluded from repr."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_id: str
    provider_name: str
    app_type: Literal["claude"]
    base_url: str
    model: str
    api_key: SecretStr = Field(repr=False)

    @classmethod
    def load(
        cls,
        database_path: Path | str = Path("~/.cc-switch/cc-switch.db"),
        *,
        provider_id: str | None = None,
        provider_name: str | None = None,
        app_type: Literal["claude"] = "claude",
    ) -> "CcSwitchProvider":
        path = Path(database_path).expanduser().resolve(strict=True)
        if provider_id and provider_name:
            raise ValueError("select a CC Switch provider by id or name, not both")
        where = "app_type = ? AND is_current = 1"
        parameters: tuple[str, ...] = (app_type,)
        if provider_id:
            where = "app_type = ? AND id = ?"
            parameters = (app_type, provider_id)
        elif provider_name:
            where = "app_type = ? AND name = ?"
            parameters = (app_type, provider_name)
        uri = f"file:{path.as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            rows = connection.execute(
                f"SELECT id, name, settings_config FROM providers WHERE {where}", parameters
            ).fetchall()
        if len(rows) != 1:
            selector = provider_id or provider_name or "current provider"
            raise LlmConfigurationError(
                f"expected one CC Switch {app_type} provider for {selector!r}, found {len(rows)}"
            )
        resolved_id, name, raw_settings = rows[0]
        try:
            environment = json.loads(raw_settings)["env"]
            return cls(
                provider_id=resolved_id,
                provider_name=name,
                app_type=app_type,
                base_url=_anthropic_sdk_base_url(environment["ANTHROPIC_BASE_URL"]),
                model=environment["ANTHROPIC_MODEL"],
                api_key=SecretStr(environment["ANTHROPIC_AUTH_TOKEN"]),
            )
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise LlmConfigurationError(
                f"CC Switch provider {name!r} is not Anthropic-compatible"
            ) from exc

    def build_chat_model(
        self,
        *,
        timeout_seconds: float = 120,
        max_retries: int = 1,
    ) -> ChatAnthropic:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        return ChatAnthropic(
            model=self.model,
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=timeout_seconds,
            temperature=0,
            max_retries=max_retries,
        )


class LangChainNarrator:
    """Generate structured prose while keeping computation outside the model."""

    SYSTEM_PROMPT = """You are the narrative component of a governed data system.
Use only the supplied metric result. Do not invent causes, entities, or values. Keep the answer
concise. Put uncertainty or source limitations in limitations. Do not use Markdown.
"""

    def __init__(self, model: Any, *, model_name: str) -> None:
        self.model = model.with_structured_output(Narrative)
        self.model_name = model_name

    def generate(self, evidence: Mapping[str, Any]) -> Narrative:
        try:
            result = self.model.invoke(
                [
                    ("system", self.SYSTEM_PROMPT),
                    ("human", json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))),
                ]
            )
        except Exception as exc:
            raise LlmInvocationError(f"model invocation failed: {type(exc).__name__}") from exc
        narrative = Narrative.model_validate(result)
        return narrative.model_copy(update={"model": self.model_name})


def _anthropic_sdk_base_url(value: str) -> str:
    """Convert CLI-style `/v1` roots to the root expected by Anthropic's SDK."""

    normalized = value.rstrip("/")
    return normalized[:-3] if normalized.endswith("/v1") else normalized
