"""Provider-neutral types: requests, responses, typed errors and the adapter protocol.

No vendor wire format appears here. The structured-output contract (schema in the prompt,
parse, validate, bounded repair) is NOT part of the adapter: it lives in `structured.py` and is
applied by `LLMClient`, so behaviour is comparable across providers and with FakeAdapter.
"""

import hashlib
import json
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


Role = Literal["system", "user", "assistant", "tool"]
NativeJson = Literal["none", "json_object", "json_schema"]


class Message(_Frozen):
    role: Role
    content: str


class ToolSpec(_Frozen):
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema for the arguments


class ToolCall(_Frozen):
    id: str | None = None
    name: str
    arguments_json: str  # raw, exactly as the provider returned it; validate before use

    def arguments(self) -> Any:
        return json.loads(self.arguments_json)


class Usage(_Frozen):
    """Token usage as reported by the provider. Every field may be missing; accounting code
    must tolerate None (and the smoke test measures how often that happens)."""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    reasoning_tokens: int | None = None


class CallContext(_Frozen):
    """Identifies the call for tracing and FakeAdapter fixture lookup. Never sent to a vendor."""

    scenario_id: str | None = None
    step: int = 0


class LLMRequest(_Frozen):
    messages: tuple[Message, ...]
    temperature: float = 0.0
    max_tokens: int = 1024
    seed: int | None = None
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    tools: tuple[ToolSpec, ...] = ()
    tool_choice: str | None = None  # None, "auto", "required", or a tool name
    tool_schema_version: str = "none"
    prompt_template_version: str = "unversioned"
    # Native structured-output hint. An OPTIMIZATION the client sets from the adapter's
    # capabilities; correctness never depends on it (our own parse/validate always runs).
    native_json: NativeJson = "none"
    native_schema: dict[str, Any] | None = None
    context: CallContext = Field(default_factory=CallContext)


class CallTrace(_Frozen):
    """What happened on one logical call. Persisted to llm_calls and returned to the caller."""

    trace_id: str
    provider: str
    model: str
    cache: Literal["hit", "miss", "off", "replay_miss"]
    outcome: Literal["ok", "error"]
    error_kind: str | None = None
    adapter_calls: int = 0  # completions requested (the 2-calls-per-case budget counts these)
    transport_attempts: int = 0  # HTTP attempts including retries after 429/5xx/timeouts
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    model_ms: float = 0.0  # time inside the provider (all attempts, excluding backoff sleeps)
    total_ms: float = 0.0
    scenario_id: str | None = None


class LLMResponse(_Frozen):
    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage = Field(default_factory=Usage)
    finish_reason: str | None = None
    model: str = ""  # as reported by the provider
    latency_ms: float = 0.0  # adapter time for the request that produced this response
    trace: CallTrace | None = None


class AdapterCapabilities(_Frozen):
    json_mode: bool = False
    json_schema: bool = False
    tool_calling: bool = False


# --- typed errors -------------------------------------------------------------------------
# Workflow mapping (see to_workflow_status): none of these is ever turned into a success.


class LLMError(Exception):
    """Base class. Carries no response; a caller that catches it has NO model output."""


class RateLimited(LLMError):
    """A per-minute style limit (requests or tokens). Retryable after a delay."""

    def __init__(self, message: str, retry_after_s: float | None = None, scope: str = "minute"):
        super().__init__(message)
        self.retry_after_s = retry_after_s
        self.scope = scope


class QuotaExhausted(LLMError):
    """The daily quota is used up (the provider said so, or our persisted counter hit the cap).
    Never retried: waiting a few seconds cannot help."""


class ProviderTimeout(LLMError):
    """No response in time. The call may or may not have been processed."""


class ProviderUnavailable(LLMError):
    """5xx, connection failure, or an adapter that is not implemented."""


class ProviderRejected(LLMError):
    """4xx other than rate limiting: our request is wrong. Not retryable."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class RequestTooLarge(LLMError):
    """The request alone exceeds a token-per-minute limit, so no wait can make it fit."""


class InvalidStructuredOutput(LLMError):
    def __init__(self, message: str, attempts: int, last_text: str):
        super().__init__(message)
        self.attempts = attempts
        self.last_text = last_text


class FixtureMissing(LLMError):
    """FakeAdapter has no fixture for this scenario/step. It never invents output."""


class CacheMiss(LLMError):
    """replay_only mode and the cache has no entry. The call is NOT made."""


class CallBudgetExceeded(LLMError):
    """The per-case LLM call budget (2) would be exceeded. Fails closed."""


def to_workflow_status(exc: LLMError) -> Literal["pending", "failed"]:
    """How an LLM failure appears in the workflow. There is deliberately no 'succeeded' here.

    pending: transient; the same request may be retried later (nothing is known to have been
    lost). failed: retrying now will not help (quota, rejected request, bad output, no budget).
    """
    if isinstance(exc, RateLimited | ProviderTimeout):
        return "pending"
    return "failed"


class LLMProvider(Protocol):
    """What an adapter implements. Raw completion only; structure is the client's job."""

    name: str
    model: str
    capabilities: AdapterCapabilities

    def complete(self, req: LLMRequest) -> LLMResponse: ...


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def prompt_hash(req: LLMRequest) -> str:
    """Hash of what the model is shown (messages and tools). Used by FakeAdapter fixtures as a
    staleness check only."""
    payload = {
        "messages": [m.model_dump() for m in req.messages],
        "tools": [t.model_dump() for t in req.tools],
    }
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def cache_key(provider: str, model: str, req: LLMRequest, kind: str, schema_json: str = "") -> str:
    """sha256 over everything that can change the output: provider, model, the full prompt,
    temperature, tool schema (content and version), prompt template version, plus the
    sampling/format knobs and the structured-output schema. A change to any of them is a miss,
    never a stale hit."""
    payload = {
        "kind": kind,
        "provider": provider,
        "model": model,
        "messages": [m.model_dump() for m in req.messages],
        "temperature": req.temperature,
        "max_tokens": req.max_tokens,
        "seed": req.seed,
        "reasoning_effort": req.reasoning_effort,
        "tools": [t.model_dump() for t in req.tools],
        "tool_choice": req.tool_choice,
        "tool_schema_version": req.tool_schema_version,
        "prompt_template_version": req.prompt_template_version,
        "native_json": req.native_json,
        "schema": schema_json,
    }
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()
