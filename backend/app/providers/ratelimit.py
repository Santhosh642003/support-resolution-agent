"""Rate limiting: token buckets, bounded exponential backoff with full jitter, and a daily
request counter persisted in PostgreSQL.

Limits are configuration (free-tier values change), never constants in code. Time, sleep and
randomness are injected so every behaviour here is tested without waiting or luck.

Daily-quota day: the DATABASE wall clock (UTC date), not the injected business Clock. Provider
quotas reset in real time, so a simulated date would make the counter never reset (or reset
wrongly). This is the same reasoning as token expiry (SPEC 2.5).
"""

import math
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import Engine, text

from app.providers.base import LLMRequest, QuotaExhausted, RateLimited, RequestTooLarge


class TokenBucket:
    """Classic token bucket. `capacity` tokens, refilled continuously at `refill_per_s`."""

    def __init__(
        self, capacity: float, refill_per_s: float, now: Callable[[], float] = time.monotonic
    ) -> None:
        self.capacity = capacity
        self.refill_per_s = refill_per_s
        self._now = now
        self._tokens = capacity
        self._last = now()

    def _refill(self) -> None:
        t = self._now()
        self._tokens = min(self.capacity, self._tokens + (t - self._last) * self.refill_per_s)
        self._last = t

    def wait_needed(self, cost: float) -> float:
        """Seconds until `cost` tokens are available (0 if available now). Does not take."""
        self._refill()
        if self._tokens >= cost:
            return 0.0
        return (cost - self._tokens) / self.refill_per_s

    def take(self, cost: float) -> None:
        self._refill()
        self._tokens -= cost

    def adjust(self, delta: float) -> None:
        """Correct an estimate after the fact (positive refunds, negative charges)."""
        self._refill()
        self._tokens = min(self.capacity, self._tokens + delta)


def estimate_tokens(req: LLMRequest) -> int:
    """Pre-call estimate: ~4 characters per token for the prompt, plus the completion cap."""
    chars = sum(len(m.content) for m in req.messages) + sum(
        len(t.model_dump_json()) for t in req.tools
    )
    return math.ceil(chars / 4) + req.max_tokens


class RateLimiter:
    """Requests-per-minute and tokens-per-minute buckets. Either may be None (unlimited)."""

    def __init__(
        self,
        rpm: int | None,
        tpm: int | None,
        *,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        max_wait_s: float = 30.0,
    ) -> None:
        self._req = TokenBucket(rpm, rpm / 60.0, now) if rpm else None
        self._tok = TokenBucket(tpm, tpm / 60.0, now) if tpm else None
        self._sleep = sleep
        self._max_wait_s = max_wait_s
        self._lock = threading.Lock()
        self.total_waited_s = 0.0

    def acquire(self, est_tokens: int) -> None:
        """Block until both buckets can pay, then take. Raises RequestTooLarge if the request
        can never fit, RateLimited(scope='local') if the wait would exceed max_wait_s."""
        if self._tok is not None and est_tokens > self._tok.capacity:
            raise RequestTooLarge(
                f"estimated {est_tokens} tokens exceeds the per-minute token limit "
                f"({int(self._tok.capacity)}); no wait can make this request fit"
            )
        waited = 0.0
        while True:
            with self._lock:
                wait = max(
                    self._req.wait_needed(1) if self._req else 0.0,
                    self._tok.wait_needed(est_tokens) if self._tok else 0.0,
                )
                if wait <= 0:
                    if self._req:
                        self._req.take(1)
                    if self._tok:
                        self._tok.take(est_tokens)
                    return
            if waited + wait > self._max_wait_s:
                raise RateLimited(
                    f"local limiter would wait {waited + wait:.1f}s (cap {self._max_wait_s}s)",
                    retry_after_s=wait,
                    scope="local",
                )
            self._sleep(wait)
            waited += wait
            self.total_waited_s += wait

    def settle(self, estimated: int, actual: int | None) -> None:
        """Replace the estimate with the provider-reported usage, when it was reported."""
        if self._tok is not None and actual is not None:
            with self._lock:
                self._tok.adjust(estimated - actual)


@dataclass(frozen=True)
class RetryPolicy:
    """Capped, bounded retries. `max_attempts` includes the first try."""

    max_attempts: int = 4
    base_delay_s: float = 1.0
    max_delay_s: float = 20.0
    total_budget_s: float = 60.0

    def delay(self, attempt: int, retry_after_s: float | None, rng: random.Random) -> float:
        """Full jitter: uniform(0, min(max, base*2^(attempt-1))). A provider Retry-After is a
        floor (never retry sooner than the provider asked)."""
        ceiling = min(self.max_delay_s, self.base_delay_s * 2 ** (attempt - 1))
        jittered = rng.uniform(0, ceiling)
        return max(jittered, retry_after_s or 0.0)


class DailyQuota:
    """Persisted daily request counter. One row per (provider, UTC day); the increment is a
    single conditional UPSERT, so concurrent processes cannot both take the last slot."""

    def __init__(self, engine: Engine, provider: str, cap: int | None) -> None:
        self._engine = engine
        self._provider = provider
        self._cap = cap

    def reserve(self) -> int:
        """Count one request against today's cap. Raises QuotaExhausted at the cap. Commits in
        its own transaction so the count survives a rollback of the caller's work."""
        if self._cap is None:
            return 0
        with self._engine.begin() as conn:
            used = conn.execute(
                text(
                    "INSERT INTO llm_usage_daily (provider, day, requests)"
                    " VALUES (:p, (clock_timestamp() AT TIME ZONE 'UTC')::date, 1)"
                    " ON CONFLICT (provider, day) DO UPDATE"
                    " SET requests = llm_usage_daily.requests + 1"
                    " WHERE llm_usage_daily.requests < :cap RETURNING requests"
                ),
                {"p": self._provider, "cap": self._cap},
            ).scalar_one_or_none()
        if used is None:
            raise QuotaExhausted(
                f"daily request quota for {self._provider} is exhausted "
                f"(configured cap {self._cap}); resets at 00:00 UTC"
            )
        return int(used)

    def used_today(self) -> int:
        with self._engine.connect() as conn:
            return int(
                conn.execute(
                    text(
                        "SELECT coalesce(max(requests), 0) FROM llm_usage_daily WHERE provider = :p"
                        " AND day = (clock_timestamp() AT TIME ZONE 'UTC')::date"
                    ),
                    {"p": self._provider},
                ).scalar_one()
            )
