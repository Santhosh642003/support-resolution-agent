"""Limiter defaults live in settings (version-controlled, visible in CI); .env only overrides."""

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine

from app.config import Settings
from app.providers.factory import build_client


def settings(**kw: Any) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[call-arg]


def test_limiter_defaults_are_version_controlled_in_settings_not_in_env() -> None:
    d = settings()
    assert (d.llm_rpm, d.llm_tpm, d.llm_rpd, d.llm_tpd) == (25, 7000, 900, 180000)
    assert d.llm_explanation_max_tokens == 1500


def test_env_overrides_the_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_TPD", "1234")
    assert settings().llm_tpd == 1234


def test_the_example_env_does_not_blank_the_limits_which_would_disable_them() -> None:
    text = (Path(__file__).resolve().parents[3] / ".env.example").read_text(encoding="utf-8")
    for name in ("LLM_RPM", "LLM_TPM", "LLM_RPD", "LLM_TPD"):
        assert not any(line.startswith(f"{name}=") for line in text.splitlines()), name


def test_real_providers_get_limiter_and_both_daily_caps_but_the_fake_gets_none(
    tmp_path: Path,
) -> None:
    engine = create_engine("postgresql+psycopg://x:x@127.0.0.1:1/x")  # never connected
    fake = build_client(settings(llm_provider="fake", llm_fixtures_dir=str(tmp_path)), engine)
    assert fake._limiter is None and fake._quota is not None and not fake._quota.enabled
    real = build_client(settings(llm_provider="groq", groq_api_key="k"), engine)
    assert real._limiter is not None and real._quota is not None and real._quota.enabled
    assert (real._quota._request_cap, real._quota._token_cap) == (900, 180000)
