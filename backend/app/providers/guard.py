"""Prompt Guard: DETECTION ONLY (CLAUDE.md, SPEC 4.0.1).

It scans the customer's message, annotates the trace and the stored message, and may raise a
non-blocking review flag. It is never the control that blocks an action and a negative result
never relaxes any check: authentication, deterministic rules and the single-use confirmation
token are the only defenses. If the guard is unavailable the turn proceeds exactly as if it were
clean-but-unscanned, and the trace says so.

Prompt Guard calls draw on their OWN provider allowance (per-model limits, SPEC 4.0.3) and are
budgeted separately from the 2-calls-per-case LLM budget. This module may name a model because it
lives in the provider layer.
"""

import time
from collections.abc import Callable, Mapping
from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict

from app.providers.adapters.groq import DEFAULT_BASE_URL
from app.providers.redact import redact

DEFAULT_GUARD_MODEL = "meta-llama/llama-prompt-guard-2-86m"
_MAX_CHARS = 1500  # the guard model has a 512-token context


class GuardVerdict(BaseModel):
    model_config = ConfigDict(frozen=True)

    available: bool
    score: float | None = None  # probability of injection / jailbreak, 0..1
    flagged: bool = False
    model: str = ""
    latency_ms: float = 0.0
    error: str | None = None


class PromptGuard(Protocol):
    def scan(self, text: str) -> GuardVerdict: ...


class FakePromptGuard:
    """Deterministic, no network. `scores` maps exact message text to a score; anything else gets
    `default`. `available=False` simulates an outage."""

    def __init__(
        self,
        scores: Mapping[str, float] | None = None,
        default: float = 0.0,
        threshold: float = 0.5,
        available: bool = True,
    ) -> None:
        self._scores = dict(scores or {})
        self._default = default
        self._threshold = threshold
        self._available = available
        self.scanned: list[str] = []

    def scan(self, text: str) -> GuardVerdict:
        self.scanned.append(text)
        if not self._available:
            return GuardVerdict(available=False, model="fake-guard", error="guard unavailable")
        score = self._scores.get(text, self._default)
        return GuardVerdict(
            available=True, score=score, flagged=score >= self._threshold, model="fake-guard"
        )


class GroqPromptGuard:
    """The Groq-hosted Llama Prompt Guard 2 classifier over the OpenAI-compatible endpoint. The
    reply is the injection score as text (for example '0.9996'). Any failure yields an
    UNAVAILABLE verdict; it never raises into the turn and never blocks it."""

    def __init__(
        self,
        api_key: str,
        model: str | None = None,
        *,
        threshold: float = 0.5,
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = 10.0,
        client: httpx.Client | None = None,
        perf: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.model = model or DEFAULT_GUARD_MODEL
        self._api_key = api_key
        self._threshold = threshold
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._client = client or httpx.Client(timeout=timeout_s)
        self._perf = perf

    def __repr__(self) -> str:
        return f"GroqPromptGuard(model={self.model!r})"

    def scan(self, text: str) -> GuardVerdict:
        started = self._perf()
        try:
            resp = self._client.post(
                self._url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": text[:_MAX_CHARS]}],
                    "max_completion_tokens": 20,
                },
            )
            ms = (self._perf() - started) * 1000
            if resp.status_code != 200:
                body = redact(resp.text[:300], self._api_key)
                return GuardVerdict(
                    available=False,
                    model=self.model,
                    latency_ms=ms,
                    error=f"HTTP {resp.status_code}: {body}",
                )
            score = float(resp.json()["choices"][0]["message"]["content"].strip())
            return GuardVerdict(
                available=True,
                score=score,
                flagged=score >= self._threshold,
                model=self.model,
                latency_ms=ms,
            )
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            return GuardVerdict(
                available=False,
                model=self.model,
                latency_ms=(self._perf() - started) * 1000,
                error=f"{type(exc).__name__}: {redact(str(exc)[:200], self._api_key)}",
            )
