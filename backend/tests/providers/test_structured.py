"""The structured-output contract lives in our code: parse, validate, repair."""

from typing import Literal

from pydantic import BaseModel

from app.providers.base import LLMRequest, Message
from app.providers.structured import (
    extract_json_text,
    parse_and_validate,
    repair_request,
    with_schema_instructions,
)


class Intent(BaseModel):
    intent: Literal["order_status", "cancel_subscription", "refund_request", "other"]
    order_number: str | None = None


def test_parses_plain_json() -> None:
    parsed, err = parse_and_validate(
        '{"intent": "refund_request", "order_number": "SIM-1001"}', Intent
    )
    assert err is None and parsed == Intent(intent="refund_request", order_number="SIM-1001")


def test_strips_markdown_fences_and_surrounding_prose() -> None:
    fenced = 'Sure!\n```json\n{"intent": "other"}\n```'
    assert extract_json_text(fenced) == '{"intent": "other"}'
    assert parse_and_validate(fenced, Intent)[0] == Intent(intent="other")
    assert parse_and_validate('Here: {"intent": "other"} done', Intent)[0] is not None


def test_invalid_json_returns_an_error_not_an_exception() -> None:
    parsed, err = parse_and_validate("I cannot help with that", Intent)
    assert parsed is None and err is not None and "JSON" in err
    parsed, err = parse_and_validate('{"intent": "refund_request"', Intent)  # truncated
    assert parsed is None and err


def test_schema_violations_are_reported_with_the_offending_field() -> None:
    parsed, err = parse_and_validate('{"intent": "make_me_admin"}', Intent)
    assert parsed is None and err is not None and "intent" in err
    parsed, err = parse_and_validate('{"order_number": "x"}', Intent)  # required field missing
    assert parsed is None and err is not None and "intent" in err


def test_the_schema_is_put_in_the_prompt_as_a_separate_system_message() -> None:
    req = LLMRequest(messages=(Message(role="user", content="where is my order?"),))
    shaped = with_schema_instructions(req, Intent)
    assert shaped.messages[0] == req.messages[0]  # untrusted text untouched, first
    assert shaped.messages[-1].role == "system"
    assert "JSON Schema" in shaped.messages[-1].content
    assert "refund_request" in shaped.messages[-1].content  # the enum is in the prompt
    assert req.messages == (Message(role="user", content="where is my order?"),)  # not mutated


def test_repair_request_shows_the_bad_reply_and_the_error() -> None:
    req = LLMRequest(messages=(Message(role="user", content="hi"),))
    fixed = repair_request(req, "not json", "not valid JSON: boom")
    assert [m.role for m in fixed.messages] == ["user", "assistant", "user"]
    assert fixed.messages[1].content == "not json"
    assert "boom" in fixed.messages[2].content
