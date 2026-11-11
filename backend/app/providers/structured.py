"""The structured-output contract, implemented in OUR code: schema in the prompt, parse,
Pydantic-validate, bounded repair. Provider-native JSON mode / tool calling is only a hint
layered on top (see LLMRequest.native_json); nothing here depends on it.
"""

import json
import re
from typing import Any

from pydantic import BaseModel, ValidationError

from app.providers.base import LLMRequest, Message

_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)


def schema_json(schema: type[BaseModel]) -> str:
    return json.dumps(schema.model_json_schema(), sort_keys=True, separators=(",", ":"))


# Keywords strict mode does not accept (or that mean nothing once everything is required).
_STRICT_DROP = ("default",)


def strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Post-process a Pydantic JSON Schema into what a provider's STRICT structured-output mode
    requires: every object gets `additionalProperties: false` and lists ALL its properties as
    `required`. Optional fields stay expressible because Pydantic already types them as
    `anyOf [T, null]`; they become required-but-nullable here, and our own Pydantic validation
    (which still treats them as optional) remains the contract. Works through `$defs`, `anyOf`,
    `items` and nested objects. Does not mutate its input. Measured against the provider on
    2026-11-11 (SPEC 4.0.2): the unmodified Pydantic schema is rejected with HTTP 400; this
    shape was accepted 20/20.
    """
    out: dict[str, Any] = json.loads(json.dumps(schema))

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key in _STRICT_DROP:
                node.pop(key, None)
            props = node.get("properties")
            if node.get("type") == "object" or isinstance(props, dict):
                node["additionalProperties"] = False
                node["required"] = list(props) if isinstance(props, dict) else []
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(out)
    return out


def with_schema_instructions(req: LLMRequest, schema: type[BaseModel]) -> LLMRequest:
    """Append the contract to the conversation as a system message. Untrusted text (customer
    messages, retrieved policy) stays in its own messages; this message only carries the format."""
    instruction = (
        "Reply with exactly ONE JSON object that conforms to the JSON Schema below. "
        "Output only that JSON: no prose, no markdown fences, no comments. "
        "Do not add properties the schema does not define.\n\nJSON Schema:\n"
        + json.dumps(schema.model_json_schema(), sort_keys=True)
    )
    return req.model_copy(
        update={"messages": (*req.messages, Message(role="system", content=instruction))}
    )


def extract_json_text(text: str) -> str:
    """Strip a markdown fence if present, then take the outermost {...}. Raises ValueError."""
    body = text.strip()
    m = _FENCE.match(body)
    if m:
        body = m.group(1).strip()
    start, end = body.find("{"), body.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in the reply")
    return body[start : end + 1]


def parse_and_validate[T: BaseModel](text: str, schema: type[T]) -> tuple[T | None, str | None]:
    """(parsed, None) on success; (None, human-readable error) on failure. Never raises."""
    try:
        payload: Any = json.loads(extract_json_text(text))
    except (ValueError, json.JSONDecodeError) as exc:
        return None, f"not valid JSON: {exc}"
    try:
        return schema.model_validate(payload), None
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in exc.errors()
        )
        return None, f"does not match the schema: {problems}"


def repair_request(req: LLMRequest, bad_text: str, error: str) -> LLMRequest:
    """The follow-up for a bounded repair: show the model its own invalid reply and the error."""
    return req.model_copy(
        update={
            "messages": (
                *req.messages,
                Message(role="assistant", content=bad_text),
                Message(
                    role="user",
                    content=(
                        f"That reply was invalid: {error}. Reply again with ONLY one JSON object "
                        "that conforms to the schema."
                    ),
                ),
            )
        }
    )
