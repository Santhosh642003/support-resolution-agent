"""Build the configured adapter and client from environment settings.

Provider and model come from LLM_PROVIDER / LLM_MODEL only. Unknown or unusable configuration
fails loudly; there is no silent fallback from one provider to another.
"""

from pathlib import Path

from sqlalchemy import Engine

from app.config import Settings
from app.providers.adapters.fake import FakeAdapter, RecordingAdapter
from app.providers.adapters.gemini import GeminiAdapter
from app.providers.adapters.groq import GroqAdapter
from app.providers.base import LLMProvider
from app.providers.cache import CacheMode
from app.providers.client import LLMClient
from app.providers.ratelimit import RateLimiter


class ProviderConfigError(RuntimeError):
    pass


def build_adapter(settings: Settings) -> LLMProvider:
    provider = settings.llm_provider
    adapter: LLMProvider
    if provider == "fake":
        if not settings.llm_fixtures_dir:
            raise ProviderConfigError("LLM_PROVIDER=fake needs LLM_FIXTURES_DIR")
        return FakeAdapter(settings.llm_fixtures_dir, model=settings.llm_model or "fake-model")
    if provider == "groq":
        if settings.groq_api_key is None or not settings.groq_api_key.get_secret_value():
            raise ProviderConfigError("LLM_PROVIDER=groq needs GROQ_API_KEY")
        adapter = GroqAdapter(settings.groq_api_key.get_secret_value(), settings.llm_model)
    elif provider == "gemini":
        key = settings.gemini_api_key.get_secret_value() if settings.gemini_api_key else None
        adapter = GeminiAdapter(key, settings.llm_model)
    else:  # pragma: no cover - Literal in Settings rules this out
        raise ProviderConfigError(f"unknown LLM_PROVIDER {provider!r}")
    if settings.llm_record:
        if not settings.llm_fixtures_dir:
            raise ProviderConfigError("RECORD=1 needs LLM_FIXTURES_DIR")
        return RecordingAdapter(adapter, Path(settings.llm_fixtures_dir))
    return adapter


def build_client(settings: Settings, engine: Engine | None) -> LLMClient:
    adapter = build_adapter(settings)
    real = settings.llm_provider != "fake"  # the fake adapter needs no throttling or quota
    limited = real and bool(settings.llm_rpm or settings.llm_tpm)
    return LLMClient(
        adapter,
        engine=engine,
        cache_mode=CacheMode(settings.llm_cache_mode) if engine is not None else CacheMode.OFF,
        limiter=RateLimiter(settings.llm_rpm, settings.llm_tpm) if limited else None,
        daily_request_cap=settings.llm_rpd if (real and engine is not None) else None,
        daily_token_cap=settings.llm_tpd if (real and engine is not None) else None,
        native_json=settings.llm_native_json,
    )
