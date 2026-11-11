"""LLMClient behaviour with no database and no network: retry/backoff, bounded repair, the
call budget, honest failure, and the model-time versus total-time split."""

from typing import Literal

import pytest
from pydantic import BaseModel

from app.providers.base import (
    CallBudgetExceeded,
    InvalidStructuredOutput,
    LLMError,
    LLMRequest,
    LLMResponse,
    Message,
    ProviderRejected,
    ProviderTimeout,
    ProviderUnavailable,
    QuotaExhausted,
    RateLimited,
    to_workflow_status,
)
from app.providers.cache import CacheMode
from app.providers.client import CallBudget, LLMClient
from app.providers.ratelimit import RateLimiter, RetryPolicy
from tests.providers.helpers import FakeTime, ScriptedAdapter, fixed_rng, reply

REQ = LLMRequest(messages=(Message(role="user", content="where is my order?"),))


class Intent(BaseModel):
    intent: Literal["order_status", "refund_request", "other"]


GOOD = '{"intent": "order_status"}'


def client(adapter: ScriptedAdapter, t: FakeTime | None = None, **kw: object) -> LLMClient:
    t = t or FakeTime()
    kw.setdefault("retry", RetryPolicy(max_attempts=4, base_delay_s=1.0, max_delay_s=8.0))
    return LLMClient(
        adapter,
        cache_mode=CacheMode.OFF,
        sleep=t.sleep,
        rng=fixed_rng(1.0),
        perf=t.now,
        **kw,  # type: ignore[arg-type]
    )


# --- retry, backoff, caps -----------------------------------------------------------------


def test_a_429_is_retried_with_exponential_backoff_and_then_succeeds() -> None:
    t = FakeTime()
    a = ScriptedAdapter([RateLimited("slow"), RateLimited("slow"), reply("fine")])
    r = client(a, t).complete(REQ)
    assert r.text == "fine" and a.calls == 3
    assert t.sleeps == [1.0, 2.0]  # base * 2^(attempt-1), top of the jitter window (rng pinned)
    assert r.trace is not None and r.trace.transport_attempts == 3 and r.trace.adapter_calls == 1


def test_retry_after_from_the_provider_is_a_floor() -> None:
    t = FakeTime()
    a = ScriptedAdapter([RateLimited("slow", retry_after_s=6.0), reply()])
    client(a, t).complete(REQ)
    assert t.sleeps == [6.0]


def test_the_retry_cap_is_hard_and_the_last_error_propagates_honestly() -> None:
    t = FakeTime()
    a = ScriptedAdapter([RateLimited("still limited")] * 4)
    with pytest.raises(RateLimited, match="still limited"):
        client(a, t).complete(REQ)
    assert a.calls == 4 and len(t.sleeps) == 3  # max_attempts, never a fifth call


def test_the_total_wall_time_budget_also_bounds_retries() -> None:
    t = FakeTime()
    a = ScriptedAdapter([RateLimited("x", retry_after_s=50.0)] * 4)
    c = client(a, t, retry=RetryPolicy(max_attempts=10, total_budget_s=60.0))
    with pytest.raises(RateLimited):
        c.complete(REQ)
    assert a.calls == 2  # 50s slept, the next 50s would exceed the 60s budget


@pytest.mark.parametrize("transient", [ProviderTimeout("t"), ProviderUnavailable("u")])
def test_timeouts_and_5xx_are_retried(transient: LLMError) -> None:
    a = ScriptedAdapter([transient, reply("recovered")])
    assert client(a).complete(REQ).text == "recovered" and a.calls == 2


def test_a_rejected_request_is_never_retried() -> None:
    a = ScriptedAdapter([ProviderRejected("bad request", 400), reply()])
    with pytest.raises(ProviderRejected):
        client(a).complete(REQ)
    assert a.calls == 1


# --- quota exhaustion: an honest failure, never a success ----------------------------------


def test_quota_exhaustion_is_not_retried_and_returns_no_response() -> None:
    t = FakeTime()
    a = ScriptedAdapter([QuotaExhausted("daily quota exhausted"), reply("must never be reached")])
    result: LLMResponse | None = None
    with pytest.raises(QuotaExhausted):
        result = client(a, t).complete(REQ)
    assert result is None and a.calls == 1 and t.sleeps == []
    assert to_workflow_status(QuotaExhausted("x")) == "failed"


def test_quota_exhaustion_also_fails_the_structured_path() -> None:
    a = ScriptedAdapter([QuotaExhausted("daily quota exhausted")])
    with pytest.raises(QuotaExhausted):
        client(a).complete_structured(REQ, Intent)


def test_every_llm_error_maps_to_pending_or_failed_never_success() -> None:
    assert to_workflow_status(RateLimited("x")) == "pending"
    assert to_workflow_status(ProviderTimeout("x")) == "pending"
    for exc in (
        QuotaExhausted("x"),
        ProviderUnavailable("x"),
        ProviderRejected("x"),
        InvalidStructuredOutput("x", 1, ""),
        CallBudgetExceeded("x"),
    ):
        assert to_workflow_status(exc) == "failed"


# --- structured output: bounded repair ----------------------------------------------------


def test_valid_structured_output_on_the_first_try() -> None:
    a = ScriptedAdapter([reply(GOOD)])
    r = client(a).complete_structured(REQ, Intent)
    assert r.parsed == Intent(intent="order_status") and r.attempts == 1 and a.calls == 1
    assert a.requests[0].messages[-1].role == "system"  # the schema went into the prompt


def test_one_repair_fixes_an_invalid_reply_and_shows_the_model_its_mistake() -> None:
    a = ScriptedAdapter([reply("sure, it's about an order"), reply(GOOD)])
    r = client(a).complete_structured(REQ, Intent)
    assert r.parsed.intent == "order_status" and r.attempts == 2 and a.calls == 2
    repair_msgs = a.requests[1].messages
    assert repair_msgs[-2].content == "sure, it's about an order"
    assert "invalid" in repair_msgs[-1].content


def test_repair_is_bounded_and_then_fails_closed() -> None:
    a = ScriptedAdapter([reply("nope"), reply("still nope"), reply(GOOD)])
    with pytest.raises(InvalidStructuredOutput) as e:
        client(a).complete_structured(REQ, Intent)
    assert a.calls == 2 and e.value.attempts == 2 and e.value.last_text == "still nope"


def test_schema_violations_trigger_repair_too() -> None:
    a = ScriptedAdapter([reply('{"intent": "grant_admin"}'), reply(GOOD)])
    assert client(a).complete_structured(REQ, Intent).attempts == 2


def test_native_json_is_only_a_hint_and_is_downgraded_to_what_the_adapter_supports() -> None:
    a = ScriptedAdapter([reply(GOOD)])
    client(a, native_json="json_schema").complete_structured(REQ, Intent)
    assert a.requests[0].native_json == "json_schema" and a.requests[0].native_schema is not None
    b = ScriptedAdapter([reply(GOOD)])
    b.capabilities = b.capabilities.model_copy(update={"json_schema": False})
    client(b, native_json="json_schema").complete_structured(REQ, Intent)
    assert b.requests[0].native_json == "json_object"
    c = ScriptedAdapter([reply(GOOD)])
    c.capabilities = c.capabilities.model_copy(update={"json_schema": False, "json_mode": False})
    client(c, native_json="json_schema").complete_structured(REQ, Intent)
    assert c.requests[0].native_json == "none"  # still works: the contract is ours


# --- the two-calls-per-case budget --------------------------------------------------------


def test_third_call_in_a_case_is_refused_before_it_is_made() -> None:
    a = ScriptedAdapter([reply("1"), reply("2"), reply("3")])
    c, budget = client(a), CallBudget(2)
    c.complete(REQ, budget=budget)
    c.complete(REQ, budget=budget)
    with pytest.raises(CallBudgetExceeded):
        c.complete(REQ, budget=budget)
    assert a.calls == 2


def test_a_repair_counts_against_the_budget_and_the_cap_fails_closed() -> None:
    a = ScriptedAdapter([reply("garbage"), reply(GOOD)])
    budget = CallBudget(2)
    budget.consume()  # extraction used one already; the explanation call will be the 2nd...
    budget.consume()  # ...and here it is spent, so no repair is allowed
    with pytest.raises(CallBudgetExceeded):
        client(a).complete_structured(REQ, Intent, budget=budget)
    assert a.calls == 0


def test_transport_retries_do_not_consume_the_call_budget() -> None:
    a = ScriptedAdapter([RateLimited("x"), RateLimited("x"), reply("ok")])
    budget = CallBudget(2)
    client(a).complete(REQ, budget=budget)
    assert budget.used == 1 and a.calls == 3


# --- limiter integration and latency accounting -------------------------------------------


def test_the_limiter_is_consulted_before_every_attempt() -> None:
    t = FakeTime()
    lim = RateLimiter(rpm=60, tpm=None, now=t.now, sleep=t.sleep)
    a = ScriptedAdapter([reply() for _ in range(62)])
    c = client(a, t, limiter=lim)
    for _ in range(62):
        c.complete(REQ)
    assert lim.total_waited_s > 0  # the 61st and 62nd had to wait for refill


def test_latency_is_split_into_model_time_and_total_time() -> None:
    t = FakeTime()

    class SlowAdapter(ScriptedAdapter):
        def complete(self, req: LLMRequest) -> LLMResponse:
            t.t += 2.0  # the "provider" takes 2 seconds per attempt
            return super().complete(req)

    a = SlowAdapter([RateLimited("x"), reply("ok")])
    r = client(a, t).complete(REQ)
    assert r.trace is not None
    # RateLimited raised inside the adapter after its 2s, then a 1s backoff, then 2s again
    assert r.trace.model_ms == pytest.approx(4000)
    assert r.trace.total_ms == pytest.approx(5000)  # includes the 1s backoff sleep
