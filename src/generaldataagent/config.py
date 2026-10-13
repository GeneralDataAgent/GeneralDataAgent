"""Runtime configuration with safe local defaults."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from GDA-prefixed environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="GDA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = "local"
    artifact_root: Path = Path(".artifacts")
    state_root: Path = Path(".state")
    default_max_runtime_seconds: int = Field(default=600, ge=1, le=86_400)
    default_max_scan_bytes: int = Field(default=10 * 1024**3, ge=0)
    default_max_tokens: int = Field(default=30_000, ge=1)
    cc_switch_db: Path = Path("~/.cc-switch/cc-switch.db")
    cc_switch_app_type: str = "claude"
    cc_switch_provider: str | None = None
    llm_timeout_seconds: float = Field(default=120, gt=0, le=600)


settings = Settings()
