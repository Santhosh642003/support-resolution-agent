"""Configuration is explicit: unusable provider settings fail loudly, with no silent fallback."""

from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.providers.adapters.fake import FakeAdapter, RecordingAdapter
from app.providers.adapters.gemini import GeminiAdapter
from app.providers.adapters.groq import GroqAdapter
from app.providers.base import LLMRequest, Message, ProviderUnavailable
from app.providers.factory import ProviderConfigError, build_adapter, build_client


def settings(**kw: Any) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[call-arg]


def test_fake_needs_a_fixtures_dir_and_builds_without_network(tmp_path: Path) -> None:
    with pytest.raises(ProviderConfigError, match="LLM_FIXTURES_DIR"):
        build_adapter(settings(llm_provider="fake"))
    a = build_adapter(settings(llm_provider="fake", llm_fixtures_dir=str(tmp_path)))
    assert isinstance(a, FakeAdapter)


def test_groq_without_a_key_fails_loudly_instead_of_falling_back() -> None:
    with pytest.raises(ProviderConfigError, match="GROQ_API_KEY"):
        build_adapter(settings(llm_provider="groq"))
    with pytest.raises(ProviderConfigError, match="GROQ_API_KEY"):
        build_adapter(settings(llm_provider="groq", groq_api_key=""))


def test_groq_builds_with_a_key_and_the_key_is_not_in_repr() -> None:
    a = build_adapter(
        settings(llm_provider="groq", groq_api_key="gsk_unit_test_key", llm_model="m")
    )
    assert isinstance(a, GroqAdapter) and a.model == "m"
    assert "gsk_unit_test_key" not in repr(a)


def test_the_settings_object_does_not_leak_the_key_either() -> None:
    s = settings(groq_api_key="gsk_unit_test_key")
    assert "gsk_unit_test_key" not in repr(s) and "gsk_unit_test_key" not in str(s.model_dump())


def test_gemini_is_a_stub_that_fails_clearly_when_called() -> None:
    a = build_adapter(settings(llm_provider="gemini"))
    assert isinstance(a, GeminiAdapter)
    with pytest.raises(ProviderUnavailable, match="not implemented"):
        a.complete(LLMRequest(messages=(Message(role="user", content="hi"),)))


def test_record_mode_wraps_the_real_adapter_and_needs_a_directory(tmp_path: Path) -> None:
    with pytest.raises(ProviderConfigError, match="RECORD"):
        build_adapter(settings(llm_provider="groq", groq_api_key="k", llm_record=True))
    a = build_adapter(
        settings(
            llm_provider="groq", groq_api_key="k", llm_record=True, llm_fixtures_dir=str(tmp_path)
        )
    )
    assert isinstance(a, RecordingAdapter)


def test_a_client_without_a_database_runs_with_the_cache_off(tmp_path: Path) -> None:
    c = build_client(settings(llm_provider="fake", llm_fixtures_dir=str(tmp_path)), engine=None)
    assert c.adapter.name == "fake"
