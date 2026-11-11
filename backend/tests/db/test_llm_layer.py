"""Cache, daily quota and call tracing against real PostgreSQL, with a scripted adapter (no
network). The cache, quota and trace tables are written in their own committed transactions (by
design: a counter must survive a rollback), so every test uses unique provider names / prompts
and scopes its assertions to them (FAILURE_ANALYSIS F-006)."""

import threading
import uuid
from typing import Literal

import pytest
from pydantic import BaseModel
from sqlalchemy import Engine, text

from app.providers.base import (
    CacheMiss,
    CallBudgetExceeded,
    InvalidStructuredOutput,
    LLMRequest,
    Message,
    QuotaExhausted,
    to_workflow_status,
)
from app.providers.cache import CacheMode, LLMCache
from app.providers.client import CallBudget, LLMClient
from app.providers.ratelimit import DailyQuota
from tests.db.helpers import db_error
from tests.providers.helpers import FakeTime, ScriptedAdapter, fixed_rng, reply


class Intent(BaseModel):
    intent: Literal["order_status", "other"]


GOOD = '{"intent": "order_status"}'


def unique_request(**over: object) -> LLMRequest:
    return LLMRequest(
        messages=(Message(role="user", content=f"prompt {uuid.uuid4().hex}"),),
        **over,  # type: ignore[arg-type]
    )


def scripted(script: list) -> ScriptedAdapter:  # type: ignore[type-arg]
    a = ScriptedAdapter(script)
    a.name = f"t-{uuid.uuid4().hex[:12]}"  # a fresh provider name isolates quota counters
    return a


def make_client(engine: Engine, adapter: ScriptedAdapter, **kw: object) -> LLMClient:
    t = FakeTime()
    return LLMClient(adapter, engine=engine, sleep=t.sleep, rng=fixed_rng(1.0), perf=t.now, **kw)  # type: ignore[arg-type]


def rows(engine: Engine, sql: str, **p: object) -> list:  # type: ignore[type-arg]
    with engine.connect() as c:
        return list(c.execute(text(sql), p))


def cache_count(engine: Engine, provider: str) -> int:
    return int(rows(engine, "SELECT count(*) FROM llm_cache WHERE provider = :p", p=provider)[0][0])


# --- cache: hits, misses, visible in the trace ------------------------------------------------


def test_second_identical_call_is_a_cache_hit_with_no_adapter_call(engine: Engine) -> None:
    a = scripted([reply("answer", total=30), reply("must not be used")])
    c, req = make_client(engine, a), unique_request()
    first = c.complete(req)
    second = c.complete(req)
    assert first.text == second.text == "answer" and a.calls == 1
    assert first.trace is not None and first.trace.cache == "miss"
    assert second.trace is not None and second.trace.cache == "hit"
    assert second.usage.total_tokens == 30  # usage replays too
    got = rows(
        engine,
        "SELECT cache_status, outcome, adapter_calls FROM llm_calls WHERE trace_id = ANY(:ids)"
        " ORDER BY id",
        ids=[first.trace.trace_id, second.trace.trace_id],
    )
    assert [tuple(r) for r in got] == [("miss", "ok", 1), ("hit", "ok", 0)]


@pytest.mark.parametrize(
    "change",
    [
        {"temperature": 0.7},
        {"prompt_template_version": "v2"},
        {"tool_schema_version": "tools-v2"},
        {"max_tokens": 77},
        {"seed": 3},
    ],
)
def test_changing_any_keyed_field_is_a_miss_never_a_stale_hit(
    engine: Engine, change: dict[str, object]
) -> None:
    a = scripted([reply("one"), reply("two")])
    c, base = make_client(engine, a), unique_request()
    c.complete(base)
    assert c.complete(base.model_copy(update=change)).text == "two" and a.calls == 2


def test_provider_and_model_are_part_of_the_key(engine: Engine) -> None:
    req = unique_request()
    a1, a2 = scripted([reply("from a1")]), scripted([reply("from a2")])
    make_client(engine, a1).complete(req)
    assert make_client(engine, a2).complete(req).text == "from a2"  # other provider: miss
    a3 = scripted([reply("from a3")])
    a3.name = a1.name
    a3.model = "another-model"
    assert make_client(engine, a3).complete(req).text == "from a3"  # same provider, other model


def test_replay_only_errors_on_a_miss_and_makes_no_call(engine: Engine) -> None:
    a = scripted([reply("never")])
    c = make_client(engine, a, cache_mode=CacheMode.REPLAY_ONLY)
    with pytest.raises(CacheMiss):
        c.complete(unique_request())
    assert a.calls == 0
    miss = rows(
        engine,
        "SELECT cache_status, outcome, error_kind FROM llm_calls WHERE provider = :p",
        p=a.name,
    )
    assert [tuple(r) for r in miss] == [("replay_miss", "error", "CacheMiss")]


def test_replay_only_returns_exactly_what_was_cached_at_zero_calls(engine: Engine) -> None:
    live = scripted([reply("snapshot")])
    req = unique_request()
    make_client(engine, live).complete(req)
    replayer = scripted([])  # any call would raise AssertionError
    replayer.name = live.name
    r = make_client(engine, replayer, cache_mode=CacheMode.REPLAY_ONLY).complete(req)
    assert r.text == "snapshot" and replayer.calls == 0 and r.trace is not None
    assert r.trace.cache == "hit"


def test_cache_off_never_reads_or_writes(engine: Engine) -> None:
    a = scripted([reply("1"), reply("2")])
    c, req = make_client(engine, a, cache_mode=CacheMode.OFF), unique_request()
    c.complete(req)
    c.complete(req)
    assert a.calls == 2 and cache_count(engine, a.name) == 0


def test_failures_and_invalid_structure_are_never_cached(engine: Engine) -> None:
    a = scripted([QuotaExhausted("daily limit")])
    with pytest.raises(QuotaExhausted):
        make_client(engine, a).complete(unique_request())
    assert cache_count(engine, a.name) == 0
    b = scripted([reply("junk"), reply("more junk")])
    with pytest.raises(InvalidStructuredOutput):
        make_client(engine, b).complete_structured(unique_request(), Intent)
    assert cache_count(engine, b.name) == 0


def test_structured_results_are_cached_after_validation_and_replay(engine: Engine) -> None:
    a = scripted([reply("not json"), reply(GOOD)])  # one repair
    c, req = make_client(engine, a), unique_request()
    first = c.complete_structured(req, Intent)
    second = c.complete_structured(req, Intent)
    assert first.attempts == 2 and second.parsed == first.parsed and a.calls == 2


def test_a_hit_consumes_the_budget_the_original_call_did(engine: Engine) -> None:
    a = scripted([reply("not json"), reply(GOOD)])  # original used 2 completions (repair)
    req = unique_request()
    make_client(engine, a).complete_structured(req, Intent)
    b1, b2 = CallBudget(2), CallBudget(1)
    replay = scripted([])
    replay.name = a.name
    c = make_client(engine, replay)
    c.complete_structured(req, Intent, budget=b1)
    assert b1.used == 2 and replay.calls == 0
    with pytest.raises(CallBudgetExceeded):
        c.complete_structured(req, Intent, budget=b2)


# --- immutability: the cache is a snapshot of one sampling -------------------------------------


def test_entries_are_immutable_and_the_first_sampling_wins(engine: Engine) -> None:
    a = scripted([reply("first sampling"), reply("second sampling")])
    req = unique_request()
    c = make_client(engine, a)
    c.complete(req)
    key = rows(engine, "SELECT cache_key FROM llm_cache WHERE provider = :p", p=a.name)[0][0]
    cache = LLMCache(engine)
    assert cache.put(key, a.name, a.model, "complete", req, reply("second sampling"), 1) is False
    stored = cache.get(key)
    assert stored is not None and stored.response.text == "first sampling"
    with engine.connect() as conn, pytest.raises(Exception) as exc:  # noqa: PT011
        conn.execute(
            text("UPDATE llm_cache SET response = '{}'::jsonb WHERE cache_key = :k"), {"k": key}
        )
    assert db_error(exc.value)[0] == "23001"  # type: ignore[arg-type]


# --- daily quota: persisted, atomic, honest ----------------------------------------------------


def test_daily_counter_stops_exactly_at_the_cap_and_survives_a_restart(engine: Engine) -> None:
    provider = f"q-{uuid.uuid4().hex[:12]}"
    q = DailyQuota(engine, provider, request_cap=3)
    assert [q.reserve(), q.reserve(), q.reserve()] == [1, 2, 3]
    with pytest.raises(QuotaExhausted, match="would be exceeded"):
        q.reserve()
    restarted = DailyQuota(engine, provider, request_cap=3)  # a new process sees the same count
    assert restarted.used_today()[0] == 3
    with pytest.raises(QuotaExhausted):
        restarted.reserve()


def test_concurrent_reservations_can_never_overspend_the_cap(engine: Engine) -> None:
    provider = f"q-{uuid.uuid4().hex[:12]}"
    cap, results, lock = 5, [], threading.Lock()

    def worker() -> None:
        try:
            DailyQuota(engine, provider, cap).reserve()
            outcome = "ok"
        except QuotaExhausted:
            outcome = "refused"
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("ok") == cap and results.count("refused") == 11
    assert DailyQuota(engine, provider, cap).used_today()[0] == cap


def test_exhausted_quota_fails_the_call_honestly_and_the_adapter_is_never_reached(
    engine: Engine,
) -> None:
    a = scripted([reply("must never be returned")])
    c = make_client(engine, a, daily_request_cap=1)
    DailyQuota(
        engine, a.name, request_cap=1
    ).reserve()  # someone already used the day's only request
    result = None
    with pytest.raises(QuotaExhausted) as exc:
        result = c.complete(unique_request())
    assert result is None and a.calls == 0
    assert to_workflow_status(exc.value) == "failed"
    assert cache_count(engine, a.name) == 0  # nothing cached from a failure
    got = rows(engine, "SELECT outcome, error_kind FROM llm_calls WHERE provider = :p", p=a.name)
    assert [tuple(r) for r in got] == [("error", "QuotaExhausted")]  # visible in the trace log


def test_quota_exhaustion_midway_through_a_case_fails_the_second_call(engine: Engine) -> None:
    """Extraction succeeds, explanation hits the daily cap: the case is failed, not faked."""
    a = scripted([reply(GOOD), reply("explanation must not exist")])
    c = make_client(engine, a, daily_request_cap=1)
    budget = CallBudget(2)
    first = c.complete_structured(unique_request(), Intent, budget=budget)
    assert first.parsed.intent == "order_status"
    with pytest.raises(QuotaExhausted):
        c.complete(unique_request(), budget=budget)
    assert a.calls == 1


def test_a_cache_hit_costs_no_quota(engine: Engine) -> None:
    a = scripted([reply("answer")])
    c, req = make_client(engine, a, daily_request_cap=1), unique_request()
    c.complete(req)  # spends the day's single request
    assert c.complete(req).text == "answer"  # served from cache: the limiter is never consulted
    assert DailyQuota(engine, a.name, 1).used_today()[0] == 1 and a.calls == 1


def test_transport_retries_each_count_against_the_daily_quota(engine: Engine) -> None:
    from app.providers.base import RateLimited

    a = scripted([RateLimited("x"), RateLimited("x"), reply("ok")])
    make_client(engine, a, daily_request_cap=10).complete(unique_request())
    assert DailyQuota(engine, a.name, 10).used_today()[0] == 3
