"""The persisted daily TOKEN counter. TPD is the binding provider limit (SPEC 4.0.2), and the local
counter is the PRIMARY control: a call that would exceed it is refused before it is made. Rows are
committed in their own transactions, so tests use unique provider names (FAILURE_ANALYSIS F-006)."""

import threading
import uuid

import pytest
from sqlalchemy import Engine, text

from app.providers.base import OutputTruncated, ProviderRejected, QuotaExhausted
from app.providers.client import LLMClient
from app.providers.ratelimit import DailyQuota
from tests.db.test_llm_layer import make_client, scripted, unique_request
from tests.providers.helpers import reply


def provider_name() -> str:
    return f"q-{uuid.uuid4().hex[:12]}"


def test_reservation_stops_at_the_cap_and_settle_returns_what_was_not_used(engine: Engine) -> None:
    q = DailyQuota(engine, provider_name(), request_cap=100, token_cap=1000)
    q.reserve(400)
    q.reserve(400)
    assert q.used_today() == (2, 800)
    with pytest.raises(QuotaExhausted, match="tokens 800/1000"):
        q.reserve(400)  # 1200 > 1000: refused BEFORE any call
    assert q.used_today() == (2, 800)  # a refusal reserves nothing
    q.settle(400, 100)  # the first call really used 100
    assert q.used_today() == (2, 500)
    q.reserve(400)  # now it fits
    assert q.used_today() == (3, 900)


def test_a_single_request_larger_than_the_whole_daily_budget_is_refused(engine: Engine) -> None:
    q = DailyQuota(engine, provider_name(), request_cap=10, token_cap=500)
    with pytest.raises(QuotaExhausted, match="exceeds the daily token budget"):
        q.reserve(501)


def test_the_first_request_of_the_day_cannot_sneak_past_the_cap(engine: Engine) -> None:
    """The INSERT path of the upsert has no WHERE; the explicit pre-check covers it."""
    q = DailyQuota(engine, provider_name(), request_cap=10, token_cap=100)
    with pytest.raises(QuotaExhausted):
        q.reserve(101)
    assert q.used_today() == (0, 0)


def test_concurrent_token_reservations_cannot_overspend(engine: Engine) -> None:
    name = provider_name()
    results: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            DailyQuota(engine, name, None, 1000).reserve(300)
            outcome = "ok"
        except QuotaExhausted:
            outcome = "refused"
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("ok") == 3  # 3 x 300 = 900 <= 1000 < 1200
    assert DailyQuota(engine, name, None, 1000).used_today()[1] == 900


def test_the_client_refuses_a_call_that_would_exceed_the_local_token_budget(
    engine: Engine,
) -> None:
    """The adapter is never reached: the provider's own daily limit is approached, not relied on."""
    a = scripted([reply("must never be returned")])
    c: LLMClient = make_client(engine, a, daily_token_cap=50)
    with pytest.raises(QuotaExhausted):
        c.complete(unique_request(max_tokens=200))  # the estimate exceeds 50
    assert a.calls == 0


def test_actual_usage_replaces_the_reservation_after_a_successful_call(engine: Engine) -> None:
    a = scripted([reply("ok", total=37)])
    make_client(engine, a, daily_token_cap=100_000).complete(unique_request())
    assert DailyQuota(engine, a.name, 10, 100_000).used_today() == (1, 37)  # settled to reported


def test_a_failed_call_returns_its_token_reservation(engine: Engine) -> None:
    a = scripted([ProviderRejected("bad request", 400)])
    with pytest.raises(ProviderRejected):
        make_client(engine, a, daily_token_cap=100_000).complete(unique_request())
    # the request counts (conservative); no tokens were consumed
    assert DailyQuota(engine, a.name, 10, 100_000).used_today() == (1, 0)


def test_a_truncated_reply_surfaces_is_traced_and_is_not_cached(engine: Engine) -> None:
    a = scripted([reply("").model_copy(update={"finish_reason": "length"})])
    with pytest.raises(OutputTruncated):
        make_client(engine, a).complete(unique_request())
    with engine.connect() as c:
        cached = c.execute(
            text("SELECT count(*) FROM llm_cache WHERE provider = :p"), {"p": a.name}
        ).scalar_one()
        calls = c.execute(
            text("SELECT outcome, error_kind FROM llm_calls WHERE provider = :p"), {"p": a.name}
        ).all()
    assert cached == 0
    assert [tuple(r) for r in calls] == [("error", "OutputTruncated")]
