"""Environment-driven configuration. No vendor SDKs or model names live here."""

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # the repo-root .env is found whether the process starts in the repo root or in backend/
        env_file=("../.env", ".env"),
        extra="ignore",
    )

    environment: Literal["dev", "test", "prod"] = "dev"
    # Business-date clock for the running app. "simulated" pins FixedClock to SIMULATED_TODAY so the
    # seeded data keeps its meaning (SPEC 2.5); "system" uses the real clock. Token and session
    # expiry always use the database wall clock, whatever this is set to.
    clock_mode: Literal["simulated", "system"] = "simulated"
    database_url: str = "postgresql+psycopg://support:support@127.0.0.1:5432/support"

    # Provider/model are selected by env only. Default is the offline fake.
    llm_provider: Literal["fake", "groq", "gemini"] = "fake"
    llm_model: str | None = None
    groq_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    # FakeAdapter fixtures (scenario ID + step index), and the manual RECORD=1 mode.
    llm_fixtures_dir: str | None = None
    llm_record: bool = Field(default=False, validation_alias=AliasChoices("RECORD", "LLM_RECORD"))
    # Provider limits. Version-controlled DEFAULTS (visible in CI); .env overrides them. From the
    # measured free-tier limits (30 RPM, 1000 RPD, 8000 TPM, 200000 TPD; SPEC 4.0.2) with
    # headroom, chosen by PM 2026-11-11. None disables a limit. Real providers only.
    llm_rpm: int | None = 25
    llm_tpm: int | None = 7000
    llm_rpd: int | None = 900
    llm_tpd: int | None = 180000
    llm_cache_mode: Literal["read_write", "replay_only", "off"] = "read_write"
    # Native structured-output hint. Only a hint: our own parse/validate always runs.
    llm_native_json: Literal["none", "json_object", "json_schema"] = "json_schema"
    # Completion cap for the explanation call. Reasoning tokens count against it, so a small cap
    # yields an EMPTY reply (finish_reason 'length'), which is treated as a failure.
    llm_explanation_max_tokens: int = 1500
    # Hybrid retrieval (app/retrieval/hybrid.py). Configuration, not constants in the code.
    # retrieval_k=7 is chosen from the recall curve in SPEC 7.1 (smallest k that retrieved every
    # clause the seeded cases need). Weights and the RRF constant are untuned defaults.
    retrieval_k: int = 7
    retrieval_candidates: int = 8
    retrieval_rrf_k: int = 60
    retrieval_weight_fts: float = 1.0
    retrieval_weight_vec: float = 1.0
    # PROJECTED cost per 1k tokens (a paid-tier estimate, never incurred cost).
    llm_price_per_1k_in: float = 0.0
    llm_price_per_1k_out: float = 0.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
