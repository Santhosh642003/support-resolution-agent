"""GroqAdapter over a fake HTTP transport: wire format, usage parsing and error mapping.
httpx.MockTransport never touches the network."""

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.providers.adapters.groq import DEFAULT_MODEL, GroqAdapter
from app.providers.base import (
    LLMRequest,
    Message,
    ProviderRejected,
    ProviderTimeout,
    ProviderUnavailable,
    QuotaExhausted,
    RateLimited,
    RequestTooLarge,
    ToolSpec,
)

SECRET = "gsk_test_secret_value_0123456789"  # noqa: S105


def make(handler: Callable[[httpx.Request], httpx.Response]) -> GroqAdapter:
    return GroqAdapter(
        SECRET, "test-model", client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def ok_body(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": "test-model",
        "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 20,
            "completion_tokens": 9,
            "total_tokens": 29,
            "completion_tokens_details": {"reasoning_tokens": 6},
        },
    }
    body.update(over)
    return body


REQ = LLMRequest(messages=(Message(role="user", content="hi"),))


def test_payload_is_openai_compatible_and_carries_the_optional_knobs() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers["authorization"]
        seen["url"] = str(request.url)
        return httpx.Response(200, json=ok_body())

    tool = ToolSpec(
        name="classify", description="d", parameters={"type": "object", "properties": {}}
    )
    req = LLMRequest(
        messages=(Message(role="user", content="hi"),),
        temperature=0.0,
        max_tokens=77,
        seed=5,
        reasoning_effort="low",
        tools=(tool,),
        tool_choice="classify",
        native_json="json_object",
    )
    make(handler).complete(req)
    b = seen["body"]
    assert seen["url"].endswith("/chat/completions") and seen["auth"] == f"Bearer {SECRET}"
    assert b["model"] == "test-model" and b["max_completion_tokens"] == 77
    assert b["seed"] == 5 and b["reasoning_effort"] == "low" and b["temperature"] == 0.0
    assert b["tools"][0]["function"]["name"] == "classify"
    assert b["tool_choice"] == {"type": "function", "function": {"name": "classify"}}
    assert b["response_format"] == {"type": "json_object"}
    assert "context" not in b  # scenario/step metadata is never sent to a vendor


def test_json_schema_mode_is_sent_strict_when_requested() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=ok_body())

    req = LLMRequest(
        messages=(Message(role="user", content="hi"),),
        native_json="json_schema",
        native_schema={"type": "object"},
    )
    make(handler).complete(req)
    rf = seen["body"]["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True


def test_parses_text_tool_calls_and_full_usage() -> None:
    body = ok_body(
        choices=[
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "classify", "arguments": '{"intent":"other"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    )
    r = make(lambda _: httpx.Response(200, json=body)).complete(REQ)
    assert r.text == "" and r.finish_reason == "tool_calls"
    assert r.tool_calls[0].name == "classify" and r.tool_calls[0].arguments() == {"intent": "other"}
    u = r.usage
    assert (u.prompt_tokens, u.completion_tokens, u.total_tokens, u.reasoning_tokens) == (
        20,
        9,
        29,
        6,
    )


def test_missing_usage_is_tolerated_not_a_crash() -> None:
    body = ok_body()
    del body["usage"]
    r = make(lambda _: httpx.Response(200, json=body)).complete(REQ)
    assert r.usage.total_tokens is None and r.usage.prompt_tokens is None


def test_default_model_comes_from_the_adapter_not_the_caller() -> None:
    assert GroqAdapter("k").model == DEFAULT_MODEL


def test_per_minute_429_is_rate_limited_and_honours_the_retry_after_header() -> None:
    body = {"error": {"message": "Rate limit reached ... on requests per minute (RPM): Limit 30"}}
    a = make(lambda _: httpx.Response(429, json=body, headers={"retry-after": "7"}))
    with pytest.raises(RateLimited) as e:
        a.complete(REQ)
    assert e.value.retry_after_s == 7.0 and e.value.scope == "minute"


def test_per_minute_429_without_a_header_falls_back_to_the_body_hint() -> None:
    body = {
        "error": {
            "message": "Rate limit reached on tokens per minute (TPM). Please try again in 1m26.4s."
        }
    }
    with pytest.raises(RateLimited) as e:
        make(lambda _: httpx.Response(429, json=body)).complete(REQ)
    assert e.value.retry_after_s == pytest.approx(86.4)


def test_daily_429_is_quota_exhausted_not_a_retryable_rate_limit() -> None:
    body = {
        "error": {"message": "Rate limit reached on requests per day (RPD): Limit 1000, Used 1000"}
    }
    with pytest.raises(QuotaExhausted):
        make(lambda _: httpx.Response(429, json=body, headers={"retry-after": "3600"})).complete(
            REQ
        )
    body2 = {"error": {"message": "limit reached on tokens per day (TPD)"}}
    with pytest.raises(QuotaExhausted):
        make(lambda _: httpx.Response(429, json=body2)).complete(REQ)


@pytest.mark.parametrize(
    ("status", "exc"),
    [
        (413, RequestTooLarge),
        (400, ProviderRejected),
        (401, ProviderRejected),
        (404, ProviderRejected),
        (500, ProviderUnavailable),
        (503, ProviderUnavailable),
    ],
)
def test_other_statuses_map_to_typed_errors(status: int, exc: type[Exception]) -> None:
    with pytest.raises(exc):
        make(lambda _: httpx.Response(status, json={"error": {"message": "nope"}})).complete(REQ)


def test_rejected_carries_the_status_code() -> None:
    with pytest.raises(ProviderRejected) as e:
        make(lambda _: httpx.Response(400, json={"error": {"message": "bad"}})).complete(REQ)
    assert e.value.status == 400


def test_timeouts_and_connection_failures_are_distinguished() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(ProviderTimeout):
        make(timeout).complete(REQ)
    with pytest.raises(ProviderUnavailable):
        make(refused).complete(REQ)


def test_a_malformed_200_body_is_unavailable_not_a_fabricated_reply() -> None:
    with pytest.raises(ProviderUnavailable):
        make(lambda _: httpx.Response(200, json={"unexpected": True})).complete(REQ)


def test_the_api_key_never_appears_in_repr_or_error_messages() -> None:
    a = make(lambda _: httpx.Response(401, json={"error": {"message": "invalid api key"}}))
    assert SECRET not in repr(a) and SECRET not in str(a.__dict__.get("model"))
    with pytest.raises(ProviderRejected) as e:
        a.complete(REQ)
    assert SECRET not in str(e.value)
