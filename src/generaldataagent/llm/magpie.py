"""Read an existing Magpie client connection without changing desktop configuration."""

import tomllib
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from generaldataagent.llm.cc_switch import LlmConfigurationError


class MagpieProvider(BaseModel):
    """Magpie's local OpenAI gateway; upstream credentials remain owned by Magpie."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_id: str = "magpie"
    provider_name: str = "Magpie"
    app_type: Literal["magpie"] = "magpie"
    base_url: str
    model: str
    wire_api: Literal["responses", "chat_completions"]
    api_key: SecretStr = Field(repr=False)
    headers: dict[str, SecretStr] = Field(default_factory=dict, repr=False)

    @classmethod
    def load(
        cls,
        config_path: Path | str = Path("~/.codex/config.toml"),
        *,
        provider_id: str | None = None,
        model: str | None = None,
    ) -> "MagpieProvider":
        # The global Codex model may belong to a different provider: require an explicit route.
        if not model:
            raise LlmConfigurationError("Magpie requires an explicit model ID")
        selector = provider_id or "magpie"
        try:
            config = tomllib.loads(Path(config_path).expanduser().read_text())
            selected = config["model_providers"][selector]
            base = selected["base_url"].rstrip("/")
            parts = urlsplit(base)
            if (
                parts.scheme not in {"http", "https"}
                or not parts.hostname
                or parts.username
                or parts.password
                or parts.query
                or parts.fragment
            ):
                raise ValueError("invalid gateway URL")
            token = selected["experimental_bearer_token"]
            headers = selected.get("http_headers", {})
            if (
                not isinstance(token, str)
                or not token.strip()
                or not isinstance(headers, dict)
                or any(
                    not isinstance(k, str)
                    or not isinstance(v, str)
                    or "\n" in k + v
                    or "\r" in k + v
                    or k.lower() in {"authorization", "host", "content-length"}
                    for k, v in headers.items()
                )
            ):
                raise ValueError("invalid gateway credentials or headers")
            return cls(
                provider_id=selector,
                base_url=base,
                model=model,
                wire_api=selected.get("wire_api", "chat_completions"),
                api_key=SecretStr(token),
                headers={k: SecretStr(v) for k, v in headers.items()},
            )
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            # Never include TOML lines, URLs, tokens or validation inputs in diagnostics.
            raise LlmConfigurationError(
                "Magpie connection configuration is unavailable or invalid"
            ) from None

    def request_headers(self) -> dict[str, str]:
        return {k: v.get_secret_value() for k, v in self.headers.items()}

    def build_chat_model(self, *, timeout_seconds: float = 120, max_retries: int = 1):
        if timeout_seconds <= 0 or max_retries < 0:
            raise ValueError("invalid model timeout or retry count")
        return ChatOpenAI(
            model=self.model,
            api_key=self.api_key,
            base_url=self.base_url,
            default_headers=self.request_headers(),
            timeout=timeout_seconds,
            max_retries=max_retries,
            temperature=0,
            use_responses_api=self.wire_api == "responses",
        )
