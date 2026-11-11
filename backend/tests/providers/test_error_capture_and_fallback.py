"""Provider error bodies are always captured (redacted), and a rejected native-JSON hint gets
exactly one retry without it, counted in the trace."""

import logging
from typing import Literal

import httpx
import pytest
from pydantic import BaseModel

from app.providers.adapters.groq import GroqAdapter
from app.providers.base import (
    LLMRequest,
    Message,
    ProviderRejected,
    RateLimited,
)
from app.providers.cache import CacheMode
from app.providers.client import CallBudget, LLMClient
from app.providers.redact import redact
from tests.providers.helpers import FakeTime, ScriptedAdapter, fixed_rng, reply

KEY = "gsk_unit_test_secret_key_0123456789abcdef"  # noqa: S105
ORG_BODY = (
    '{"error": {"message": "Rate limit reached for model m in organization org_01abc123def on '
    'requests per minute (RPM). Used 30", "type": "requests", "code": "rate_limit_exceeded"}}'
)
REQ = LLMRequest(messages=(Message(role="user", content="hi"),))


class Intent(BaseModel):
    intent: Literal["a", "b"]


GOOD = '{"intent": "a"}'


def adapter(status: int, body: str) -> GroqAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, content=body.encode(), headers={"content-type": "application/json"}
        )

    return GroqAdapter(
        KEY, "test-model", client=httpx.Client(transport=httpx.MockTransport(handler))
    )


# --- redaction -------------------------------------------------------------------------------


def test_redact_removes_keys_bearer_tokens_org_ids_and_known_secrets() -> None:
    text = f"Bearer abcdefghijklmnop {KEY} org_01xyz789 AIzaSyA1234567890abcdefghij custom-secret-7"
    out = redact(text, "custom-secret-7")
    for leaked in ("abcdefghijklmnop", KEY, "org_01xyz789", "AIzaSyA1234567890", "custom-secret-7"):
        assert leaked not in out
    assert out.count("[REDACTED]") >= 4


# --- the error body is ALWAYS captured, in full, redacted --------------------------------------


def test_a_400_body_is_logged_and_attached_to_the_exception_without_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = (
        '{"error": {"message": "json_validate_failed: model output did not match the schema for '
        f'organization org_01abc123def", "type": "invalid_request_error", "debug": "{KEY}"}}}}'
    )
    with caplog.at_level(logging.WARNING), pytest.raises(ProviderRejected) as exc:
        adapter(400, body).complete(REQ)
    assert exc.value.status == 400
    assert exc.value.detail is not None and "json_validate_failed" in exc.value.detail  # captured
    assert "invalid_request_error" in exc.value.detail  # the whole body, not just the message
    for text in (exc.value.detail, str(exc.value), caplog.text):
        assert KEY not in text and "org_01abc123def" not in text, text
    assert "json_validate_failed" in caplog.text  # and it was logged


@pytest.mark.parametrize("status", [413, 429, 500, 503])
def test_every_error_status_carries_a_redacted_detail(status: int) -> None:
    with pytest.raises(Exception) as exc:  # noqa: PT011
        adapter(status, ORG_BODY).complete(REQ)
    detail = exc.value.detail  # type: ignore[attr-defined]
    assert detail and "org_01abc123def" not in detail and "organization" in detail
    assert "org_01abc123def" not in str(exc.value)


def test_exception_messages_never_contain_the_organisation_id() -> None:
    with pytest.raises(RateLimited) as exc:
        adapter(429, ORG_BODY).complete(REQ)
    assert "org_01abc123def" not in str(exc.value)


# --- the 400 fallback ---------------------------------------------------------------------------


def client(a: ScriptedAdapter, native: str = "json_schema") -> LLMClient:
    t = FakeTime()
    return LLMClient(
        a,
        cache_mode=CacheMode.OFF,
        native_json=native,  # type: ignore[arg-type]
        sleep=t.sleep,
        rng=fixed_rng(1.0),
        perf=t.now,
    )


def rejected(detail: str = "json_validate_failed") -> ProviderRejected:
    err = ProviderRejected("rejected (400)", 400)
    err.detail = detail
    return err


def test_a_rejected_native_hint_is_retried_once_without_it_and_counted() -> None:
    a = ScriptedAdapter([rejected(), reply(GOOD)])
    budget = CallBudget(2)
    r = client(a).complete_structured(REQ, Intent, budget=budget)
    assert r.parsed == Intent(intent="a")
    assert a.requests[0].native_json == "json_schema" and a.requests[0].native_schema is not None
    assert a.requests[1].native_json == "none" and a.requests[1].native_schema is None
    assert r.raw.trace is not None and r.raw.trace.native_fallbacks == 1
    assert budget.used == 1  # the rejected attempt produced no output: not an LLM call
    assert a.requests[1].messages == a.requests[0].messages  # same prompt, contract unchanged


def test_the_fallback_happens_at_most_once_and_the_second_error_is_reported() -> None:
    a = ScriptedAdapter([rejected("first"), rejected("second"), reply(GOOD)])
    with pytest.raises(ProviderRejected) as exc:
        client(a).complete_structured(REQ, Intent)
    assert a.calls == 2 and exc.value.detail == "second"  # not about the native hint: surfaced


def test_other_4xx_statuses_do_not_trigger_the_fallback() -> None:
    for status in (401, 403, 404):
        a = ScriptedAdapter([ProviderRejected("nope", status), reply(GOOD)])
        with pytest.raises(ProviderRejected):
            client(a).complete_structured(REQ, Intent)
        assert a.calls == 1


def test_there_is_no_fallback_when_no_native_hint_was_sent() -> None:
    a = ScriptedAdapter([rejected(), reply(GOOD)])
    with pytest.raises(ProviderRejected):
        client(a, native="none").complete_structured(REQ, Intent)
    assert a.calls == 1


def test_a_later_repair_does_not_bring_the_native_hint_back() -> None:
    a = ScriptedAdapter([rejected(), reply("not json"), reply(GOOD)])
    client(a).complete_structured(REQ, Intent)
    assert [r.native_json for r in a.requests] == ["json_schema", "none", "none"]


def test_a_call_that_never_falls_back_reports_zero() -> None:
    r = client(ScriptedAdapter([reply(GOOD)])).complete_structured(REQ, Intent)
    assert r.raw.trace is not None and r.raw.trace.native_fallbacks == 0
