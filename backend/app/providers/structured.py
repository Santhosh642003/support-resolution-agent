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
