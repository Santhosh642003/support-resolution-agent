"""Environment-driven configuration. No vendor SDKs or model names live here."""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: Literal["dev", "test", "prod"] = "dev"
    database_url: str = "postgresql+psycopg://support:support@127.0.0.1:5432/support"

    # Provider/model are selected by env only. Default is the offline fake.
    llm_provider: Literal["fake", "groq", "gemini"] = "fake"
    llm_model: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
