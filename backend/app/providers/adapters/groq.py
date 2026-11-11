"""Groq via its OpenAI-compatible HTTP API, using httpx directly (no vendor SDK).

This module and its siblings are the only place a model name may appear. The API key is held
only in the request headers; it is never logged and never part of repr().
"""

import re
import time
from collections.abc import Callable
from typing import Any

import httpx

from app.providers.base import (
    AdapterCapabilities,
    LLMRequest,
    LLMResponse,
    ProviderRejected,
    ProviderTimeout,
    ProviderUnavailable,
    QuotaExhausted,
    RateLimited,
    RequestTooLarge,
    ToolCall,
    Usage,
)

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "openai/gpt-oss-120b"  # a default only; LLM_MODEL overrides it

_TRY_AGAIN = re.compile(r"try again in\s+(?:(\d+)m)?\s*([\d.]+)s", re.IGNORECASE)
# UNVERIFIED INFERENCE, NOT LOAD-BEARING. The daily-limit 429 body was never observed (provoking it
# would have spent the whole day's allowance); this pattern guesses it from the wording of the
# per-minute errors that WERE observed ("... on requests per minute (RPM) ..."). The primary quota
# control is the local persisted counter (DailyQuota), which refuses a call before it is made. This
# parsing is only a secondary signal for the case where the provider disagrees with our counter.
_DAILY = re.compile(r"per day|\((?:rpd|tpd)\)", re.IGNORECASE)


def _message_of(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        err = body.get("error", body) if isinstance(body, dict) else body
        msg = err.get("message") if isinstance(err, dict) else None
        return str(msg if msg else body)[:500]
    except ValueError:
        return resp.text[:500]


def _retry_after_s(resp: httpx.Response, message: str) -> float | None:
    header = resp.headers.get("retry-after")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    m = _TRY_AGAIN.search(message)
    if m:
        return int(m.group(1) or 0) * 60 + float(m.group(2))
    return None


class GroqAdapter:
    name = "groq"
    capabilities = AdapterCapabilities(json_mode=True, json_schema=True, tool_calling=True)

    def __init__(
        self,
        api_key: str,
        model: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = 60.0,
        client: httpx.Client | None = None,
        on_response: Callable[[httpx.Response], None] | None = None,
    ) -> None:
        self.model = model or DEFAULT_MODEL
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self._client = client or httpx.Client(timeout=timeout_s)
        self._on_response = on_response  # observability hook (smoke test): sees headers

    def __repr__(self) -> str:
        return f"GroqAdapter(model={self.model!r}, base_url={self._base_url!r})"

    def _payload(self, req: LLMRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [m.model_dump() for m in req.messages],
            "temperature": req.temperature,
            "max_completion_tokens": req.max_tokens,
        }
        if req.seed is not None:
            body["seed"] = req.seed
        if req.reasoning_effort is not None:
            body["reasoning_effort"] = req.reasoning_effort
        if req.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in req.tools
            ]
            if req.tool_choice in ("auto", "required", "none"):
                body["tool_choice"] = req.tool_choice
            elif req.tool_choice:
                body["tool_choice"] = {"type": "function", "function": {"name": req.tool_choice}}
        if req.native_json == "json_object":
            body["response_format"] = {"type": "json_object"}
        elif req.native_json == "json_schema" and req.native_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "reply", "strict": True, "schema": req.native_schema},
            }
        return body

    def complete(self, req: LLMRequest) -> LLMResponse:
        started = time.monotonic()
        try:
            resp = self._client.post(
                f"{self._base_url}/chat/completions", headers=self._headers, json=self._payload(req)
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"no response from {self.name}: {type(exc).__name__}") from exc
        except httpx.TransportError as exc:
            raise ProviderUnavailable(f"cannot reach {self.name}: {type(exc).__name__}") from exc
        elapsed_ms = (time.monotonic() - started) * 1000
        if self._on_response is not None:
            self._on_response(resp)
        return self._interpret(resp, elapsed_ms)

    def _interpret(self, resp: httpx.Response, elapsed_ms: float) -> LLMResponse:
        status = resp.status_code
        if status == 200:
            return self._parse_ok(resp, elapsed_ms)
        message = _message_of(resp)
        if status == 429:
            if _DAILY.search(message):
                raise QuotaExhausted(f"{self.name} daily quota exhausted: {message}")
            raise RateLimited(message, retry_after_s=_retry_after_s(resp, message), scope="minute")
        if status == 413:
            raise RequestTooLarge(message)
        if status >= 500:
            raise ProviderUnavailable(f"{self.name} returned {status}: {message}")
        raise ProviderRejected(f"{self.name} rejected the request ({status}): {message}", status)

    def _parse_ok(self, resp: httpx.Response, elapsed_ms: float) -> LLMResponse:
        try:
            data = resp.json()
            choice = data["choices"][0]
            msg = choice["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderUnavailable(f"{self.name} returned a malformed 200 body") from exc
        raw_usage = data.get("usage") or {}
        details = raw_usage.get("completion_tokens_details") or {}
        calls = tuple(
            ToolCall(
                id=tc.get("id"),
                name=tc["function"]["name"],
                arguments_json=tc["function"].get("arguments") or "",
            )
            for tc in (msg.get("tool_calls") or [])
        )
        return LLMResponse(
            text=msg.get("content") or "",
            tool_calls=calls,
            usage=Usage(
                prompt_tokens=raw_usage.get("prompt_tokens"),
                completion_tokens=raw_usage.get("completion_tokens"),
                total_tokens=raw_usage.get("total_tokens"),
                reasoning_tokens=details.get("reasoning_tokens"),
            ),
            finish_reason=choice.get("finish_reason"),
            model=str(data.get("model", "")),
            latency_ms=elapsed_ms,
        )
