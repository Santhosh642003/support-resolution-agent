"""Environment-driven configuration. No vendor SDKs or model names live here."""

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: Literal["dev", "test", "prod"] = "dev"
    database_url: str = "postgresql+psycopg://support:support@127.0.0.1:5432/support"

    # Provider/model are selected by env only. Default is the offline fake.
    llm_provider: Literal["fake", "groq", "gemini"] = "fake"
    llm_model: str | None = None
    groq_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    # FakeAdapter fixtures (scenario ID + step index), and the manual RECORD=1 mode.
    llm_fixtures_dir: str | None = None
    llm_record: bool = Field(default=False, validation_alias=AliasChoices("RECORD", "LLM_RECORD"))
    llm_cache_mode: Literal["read_write", "replay_only", "off"] = "read_write"
    # Free-tier limits change and live in config, not code. None = unlimited/unknown.
    llm_rpm: int | None = None
    llm_tpm: int | None = None
    llm_rpd: int | None = None
    # PROJECTED cost per 1k tokens (a paid-tier estimate, never incurred cost).
    llm_price_per_1k_in: float = 0.0
    llm_price_per_1k_out: float = 0.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
