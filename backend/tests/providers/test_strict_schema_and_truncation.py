"""The strict json_schema helper (measured: unmodified Pydantic schemas are rejected with HTTP 400,
SPEC 4.0.2) and the 'empty reply with finish_reason length' failure."""

from typing import Any, Literal

import pytest
from pydantic import BaseModel

from app.providers.base import (
    LLMRequest,
    LLMResponse,
    Message,
    OutputTruncated,
    ToolCall,
    to_workflow_status,
)
from app.providers.cache import CacheMode
from app.providers.client import CallBudget, LLMClient
from app.providers.structured import parse_and_validate, strict_json_schema
from tests.providers.helpers import FakeTime, ScriptedAdapter, fixed_rng, reply

REQ = LLMRequest(messages=(Message(role="user", content="hello"),))


class Line(BaseModel):
    sku: str
    qty: int = 1


class Nested(BaseModel):
    intent: Literal["a", "b"]
    note: str | None = None
    lines: list[Line] = []
    primary: Line | None = None


def _objects(node: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if node.get("type") == "object" or "properties" in node:
            found.append(node)
        for v in node.values():
            found.extend(_objects(v))
    elif isinstance(node, list):
        for v in node:
            found.extend(_objects(v))
    return found


def test_strict_schema_closes_and_requires_every_object_including_nested_and_defs() -> None:
    raw = Nested.model_json_schema()
    assert "additionalProperties" not in raw  # the reason the provider returned HTTP 400
    strict = strict_json_schema(raw)
    objs = _objects(strict)
    assert len(objs) >= 2  # the root and Line (under $defs)
    for o in objs:
        assert o["additionalProperties"] is False
        assert o["required"] == list(o["properties"])
    assert strict["required"] == ["intent", "note", "lines", "primary"]  # optionals included


def test_strict_schema_drops_defaults_keeps_nullability_and_enums_and_does_not_mutate() -> None:
    raw = Nested.model_json_schema()
    before = repr(raw)
    strict = strict_json_schema(raw)
    assert repr(raw) == before  # input untouched
    assert "default" not in repr(strict)
    assert strict["properties"]["intent"]["enum"] == ["a", "b"]
    assert {"type": "null"} in strict["properties"]["note"]["anyOf"]  # optional stays nullable


def test_strict_schema_is_idempotent() -> None:
    once = strict_json_schema(Nested.model_json_schema())
    assert strict_json_schema(once) == once


def test_our_pydantic_contract_still_accepts_what_strict_mode_makes_the_model_emit() -> None:
    """Strict mode makes the model emit every key (null for optionals); Pydantic must accept it."""
    parsed, err = parse_and_validate(
        '{"intent": "a", "note": null, "lines": [], "primary": null}', Nested
    )
    assert err is None and parsed == Nested(intent="a")


def test_the_client_sends_the_strict_form_when_json_schema_mode_is_chosen() -> None:
    a = ScriptedAdapter([reply('{"intent": "a"}')])
    t = FakeTime()
    c = LLMClient(
        a,
        cache_mode=CacheMode.OFF,
        native_json="json_schema",
        sleep=t.sleep,
        rng=fixed_rng(1.0),
        perf=t.now,
    )
    c.complete_structured(REQ, Nested)
    sent = a.requests[0].native_schema
    assert sent is not None and sent["additionalProperties"] is False
    assert sent == strict_json_schema(Nested.model_json_schema())


# --- an empty reply with finish_reason 'length' is a failure that surfaces ------------------


def truncated() -> LLMResponse:
    return reply("").model_copy(update={"finish_reason": "length"})


def client(a: ScriptedAdapter) -> LLMClient:
    t = FakeTime()
    return LLMClient(a, cache_mode=CacheMode.OFF, sleep=t.sleep, rng=fixed_rng(1.0), perf=t.now)


def test_empty_reply_with_finish_reason_length_raises_and_is_not_retried() -> None:
    a = ScriptedAdapter([truncated(), reply("a silent retry would return this")])
    with pytest.raises(OutputTruncated, match="max_tokens"):
        client(a).complete(REQ)
    assert a.calls == 1  # never a silent retry
    assert to_workflow_status(OutputTruncated("x")) == "failed"


def test_truncation_fails_the_structured_path_and_the_spent_call_is_not_hidden() -> None:
    a = ScriptedAdapter([truncated()])
    budget = CallBudget(2)
    with pytest.raises(OutputTruncated):
        client(a).complete_structured(REQ, Nested, budget=budget)
    assert budget.used == 1


def test_partial_text_with_length_is_not_this_failure() -> None:
    """Only EMPTY + length is the reasoning-ate-the-cap case; partial text is a different
    problem (structured output fails validation and uses its bounded repair)."""
    partial = reply("half an answ").model_copy(update={"finish_reason": "length"})
    assert client(ScriptedAdapter([partial])).complete(REQ).text == "half an answ"


def test_a_tool_call_with_no_text_is_not_mistaken_for_an_empty_reply() -> None:
    call = ToolCall(name="classify", arguments_json="{}")
    r = reply("", tool_calls=(call,)).model_copy(update={"finish_reason": "length"})
    assert client(ScriptedAdapter([r])).complete(REQ).tool_calls == (call,)
