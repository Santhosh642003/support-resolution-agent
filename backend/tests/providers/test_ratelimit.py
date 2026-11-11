"""Token buckets, backoff with jitter, and the limiter's hard bounds. No real waiting."""

import pytest

from app.providers.base import LLMRequest, Message, RateLimited, RequestTooLarge
from app.providers.ratelimit import RateLimiter, RetryPolicy, TokenBucket, estimate_tokens
from tests.providers.helpers import FakeTime, fixed_rng


def test_bucket_starts_full_drains_and_refills_at_the_configured_rate() -> None:
    t = FakeTime()
    b = TokenBucket(capacity=10, refill_per_s=2, now=t.now)
    assert b.wait_needed(10) == 0
    b.take(10)
    assert b.wait_needed(4) == pytest.approx(2.0)  # 4 tokens at 2/s
    t.t += 1.0
    assert b.wait_needed(4) == pytest.approx(1.0)
    t.t += 100
    assert b.wait_needed(10) == 0  # never exceeds capacity


def test_limiter_spaces_requests_to_the_rpm() -> None:
    t = FakeTime()
    lim = RateLimiter(rpm=60, tpm=None, now=t.now, sleep=t.sleep)  # 1 per second, burst 60
    for _ in range(60):
        lim.acquire(1)
    assert t.sleeps == []  # the burst allowance
    lim.acquire(1)
    assert t.sleeps == [pytest.approx(1.0)]  # the 61st request waits one refill interval


def test_limiter_honours_the_token_budget_and_settles_with_actual_usage() -> None:
    t = FakeTime()
    lim = RateLimiter(rpm=None, tpm=600, now=t.now, sleep=t.sleep)  # 10 tokens/s
    lim.acquire(500)
    lim.settle(estimated=500, actual=100)  # the call used far less than estimated: refund 400
    lim.acquire(500)  # fits without waiting because of the refund
    assert t.sleeps == []


def test_a_request_larger_than_the_per_minute_budget_can_never_fit() -> None:
    lim = RateLimiter(rpm=None, tpm=1000)
    with pytest.raises(RequestTooLarge):
        lim.acquire(1001)


def test_waits_are_capped_so_the_limiter_cannot_stall_a_request_forever() -> None:
    t = FakeTime()
    lim = RateLimiter(rpm=1, tpm=None, now=t.now, sleep=t.sleep, max_wait_s=5)
    lim.acquire(1)
    with pytest.raises(RateLimited) as e:  # next slot is 60s away, cap is 5s
        lim.acquire(1)
    assert e.value.scope == "local" and t.sleeps == []


def test_estimate_counts_prompt_and_completion_cap() -> None:
    req = LLMRequest(messages=(Message(role="user", content="x" * 400),), max_tokens=100)
    assert estimate_tokens(req) == 100 + 100


def test_backoff_is_exponential_with_full_jitter_and_bounded() -> None:
    policy = RetryPolicy(max_attempts=4, base_delay_s=1.0, max_delay_s=5.0)
    top = fixed_rng(1.0)  # always the ceiling of the jitter window
    assert [policy.delay(a, None, top) for a in (1, 2, 3, 4, 5)] == [1.0, 2.0, 4.0, 5.0, 5.0]
    bottom = fixed_rng(0.0)
    assert policy.delay(3, None, bottom) == 0.0  # full jitter really spans [0, ceiling]


def test_retry_after_is_a_floor_never_undercut_by_jitter() -> None:
    policy = RetryPolicy(base_delay_s=1.0, max_delay_s=5.0)
    assert policy.delay(1, 12.0, fixed_rng(0.0)) == 12.0
    assert policy.delay(1, 0.5, fixed_rng(1.0)) == 1.0
