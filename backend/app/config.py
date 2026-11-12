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
    # retrieval_k=6 (PM, 2026-11-13): the smallest k at which the pre-registered rule R3 is met by
    # the system we kept (equal-weight hybrid). Held-out scored 2 queries better at k=7 (29/32
    # against 27/32); that was deliberately NOT used. Fusion weights are unchanged: the registered
    # fusion rule could not discriminate (ceiling effect), so no change was made (SPEC 7.3).
    # Candidates >= k so each leg can supply a full top-k; the RRF constant is untuned.
    retrieval_k: int = 6
    retrieval_candidates: int = 20
    retrieval_rrf_k: int = 60
    retrieval_weight_fts: float = 1.0
    # Full-text leg configuration B (SPEC 7.3): AND matches first then OR fill, ts_rank_cd, title
    # weighted. Chosen on the selection set by the pre-registered rule R1 (13/16 against 5/16 for
    # the original OR / ts_rank leg). The ORIGINAL leg stays available as BASELINE_LEXICAL for
    # reproducing earlier numbers.
    retrieval_fts_form: Literal["or", "and", "and_then_or"] = "and_then_or"
    retrieval_fts_rank: Literal["rank", "rank_cd"] = "rank_cd"
    retrieval_fts_title_weight: bool = True
    retrieval_weight_vec: float = 1.0
    # Prompt Guard (detection only; its own provider allowance, SPEC 4.0.1/4.0.3).
    prompt_guard_enabled: bool = True
    prompt_guard_threshold: float = 0.5
    # PROJECTED cost per 1k tokens (a paid-tier estimate, never incurred cost).
    llm_price_per_1k_in: float = 0.0
    llm_price_per_1k_out: float = 0.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
