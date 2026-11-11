"""LLMClient: the one entry point the rest of the project uses.

    cache lookup -> [hit: return] -> daily quota -> rate limiter -> adapter -> retry/backoff
        -> validate structure (bounded repair) -> cache write -> trace/usage logging

The cache sits OUTSIDE the limiter, so a hit consumes no quota. Errors are typed and propagate;
no code path turns a failure into a response. Time, sleep and randomness are injected.

Call budget: at most 2 LLM calls per case (CLAUDE.md). A `CallBudget` is shared by the calls of
one case. Every completion requested counts, repair completions included; transport retries after
429/5xx/timeouts do not (they produce no output). A cache hit consumes the same number of calls
the original did, so a replay behaves exactly like the live run, including hitting the cap.
"""

import logging
import random
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.providers.base import (
    CacheMiss,
    CallBudgetExceeded,
    CallTrace,
    InvalidStructuredOutput,
    LLMError,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    NativeJson,
    ProviderTimeout,
    ProviderUnavailable,
    QuotaExhausted,
    RateLimited,
    cache_key,
)
from app.providers.cache import CacheMode, LLMCache
from app.providers.ratelimit import DailyQuota, RateLimiter, RetryPolicy, estimate_tokens
from app.providers.structured import (
    parse_and_validate,
    repair_request,
    schema_json,
    with_schema_instructions,
)

log = logging.getLogger(__name__)
CALLS_PER_CASE = 2


@dataclass
class _Stats:
    model_s: float = 0.0
    adapter_calls: int = 0
    transport: int = 0


class CallBudget:
    """Per-case LLM call budget. Not thread-safe: one case runs on one thread."""

    def __init__(self, limit: int = CALLS_PER_CASE) -> None:
        self.limit = limit
        self.used = 0

    def consume(self, n: int = 1) -> None:
        if self.used + n > self.limit:
            raise CallBudgetExceeded(
                f"LLM call budget exceeded: {self.used} used + {n} requested > {self.limit}"
            )
        self.used += n


class StructuredResponse[T: BaseModel](BaseModel):
    model_config = ConfigDict(frozen=True)
    parsed: T
    raw: LLMResponse
    attempts: int  # completions it took (1 = valid first time; 2 = one repair)


class LLMClient:
    def __init__(
        self,
        adapter: LLMProvider,
        *,
        engine: Engine | None = None,
        cache_mode: CacheMode = CacheMode.READ_WRITE,
        limiter: RateLimiter | None = None,
        daily_cap: int | None = None,
        retry: RetryPolicy | None = None,
        native_json: NativeJson = "none",
        max_repairs: int = 1,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        perf: Callable[[], float] = time.perf_counter,
    ) -> None:
        if cache_mode is not CacheMode.OFF and engine is None:
            raise ValueError("a cache (read_write or replay_only) needs a database engine")
        if daily_cap is not None and engine is None:
            raise ValueError("a persisted daily cap needs a database engine")
        self.adapter = adapter
        self._engine = engine
        self._cache = LLMCache(engine, cache_mode) if engine is not None else None
        self._limiter = limiter
        self._quota = DailyQuota(engine, adapter.name, daily_cap) if engine is not None else None
        self._retry = retry or RetryPolicy()
        self._native_json = native_json
        self._max_repairs = max_repairs
        self._sleep = sleep
        self._rng = rng or random.Random()  # noqa: S311 (backoff jitter only; not security)
        self._perf = perf
        self._mode = cache_mode

    # --- public API ----------------------------------------------------------------------

    def complete(self, req: LLMRequest, *, budget: CallBudget | None = None) -> LLMResponse:
        key = cache_key(self.adapter.name, self.adapter.model, req, "complete")
        return self._run(req, key, "complete", None, budget)[0]

    def complete_structured[T: BaseModel](
        self, req: LLMRequest, schema: type[T], *, budget: CallBudget | None = None
    ) -> StructuredResponse[T]:
        native: NativeJson = self._native_json
        if native == "json_schema" and not self.adapter.capabilities.json_schema:
            native = "json_object"
        if native == "json_object" and not self.adapter.capabilities.json_mode:
            native = "none"
        shaped = with_schema_instructions(req, schema).model_copy(
            update={
                "native_json": native,
                "native_schema": schema.model_json_schema() if native == "json_schema" else None,
            }
        )
        key = cache_key(
            self.adapter.name, self.adapter.model, shaped, "structured", schema_json(schema)
        )
        resp, parsed, attempts = self._run(shaped, key, "structured", schema, budget)
        if parsed is None:  # cannot happen: _run raises unless structured parsing succeeded
            raise InvalidStructuredOutput("no parsed value", 0, resp.text)
        return StructuredResponse[T](parsed=parsed, raw=resp, attempts=attempts)

    # --- the pipeline --------------------------------------------------------------------

    def _run[T: BaseModel](
        self,
        req: LLMRequest,
        key: str,
        kind: Literal["complete", "structured"],
        schema: type[T] | None,
        budget: CallBudget | None,
    ) -> tuple[LLMResponse, T | None, int]:
        started = self._perf()
        state = _Stats()
        trace_id = uuid.uuid4().hex
        cache_status: Literal["hit", "miss", "off", "replay_miss"] = (
            "off" if self._mode is CacheMode.OFF else "miss"
        )
        resp: LLMResponse | None = None
        try:
            if self._cache is not None and self._mode is not CacheMode.OFF:
                entry = self._cache.get(key)
                if entry is not None:
                    cache_status = "hit"
                    if budget:
                        budget.consume(entry.adapter_calls)
                    resp = entry.response
                    parsed = None
                    if schema is not None:
                        parsed, err = parse_and_validate(resp.text, schema)
                        if parsed is None:  # cannot happen for entries we wrote; fail closed
                            raise InvalidStructuredOutput(
                                f"cached entry no longer validates: {err}", 0, resp.text
                            )
                    state.adapter_calls = 0
                    return (
                        self._finish(resp, req, trace_id, cache_status, state, started, None, kind),
                        parsed,
                        entry.adapter_calls,
                    )
                if self._mode is CacheMode.REPLAY_ONLY:
                    cache_status = "replay_miss"
                    raise CacheMiss(
                        "replay_only: no cached response for this request; no call made"
                    )

            resp, parsed, attempts = self._fresh(req, kind, schema, budget, state)
            if self._cache is not None and self._mode is CacheMode.READ_WRITE:
                self._cache.put(
                    key, self.adapter.name, self.adapter.model, kind, req, resp, attempts
                )
            return (
                self._finish(resp, req, trace_id, cache_status, state, started, None, kind),
                parsed,
                attempts,
            )
        except LLMError as exc:
            self._log_trace(
                CallTrace(
                    trace_id=trace_id,
                    provider=self.adapter.name,
                    model=self.adapter.model,
                    cache=cache_status,
                    outcome="error",
                    error_kind=type(exc).__name__,
                    adapter_calls=state.adapter_calls,
                    transport_attempts=state.transport,
                    model_ms=state.model_s * 1000,
                    total_ms=(self._perf() - started) * 1000,
                    scenario_id=req.context.scenario_id,
                )
            )
            raise

    def _fresh[T: BaseModel](
        self,
        req: LLMRequest,
        kind: str,
        schema: type[T] | None,
        budget: CallBudget | None,
        state: _Stats,
    ) -> tuple[LLMResponse, T | None, int]:
        attempts = 0
        current = req
        while True:
            if budget:
                budget.consume()
            attempts += 1
            state.adapter_calls += 1
            resp = self._dispatch(current, state)
            if schema is None:
                return resp, None, attempts
            parsed, err = parse_and_validate(resp.text, schema)
            if parsed is not None:
                return resp, parsed, attempts
            if attempts > self._max_repairs:
                raise InvalidStructuredOutput(
                    f"structured output invalid after {attempts} attempt(s): {err}",
                    attempts,
                    resp.text,
                )
            log.warning("structured output invalid (%s); repair attempt %d", err, attempts)
            current = repair_request(current, resp.text, err or "invalid")

    def _dispatch(self, req: LLMRequest, state: _Stats) -> LLMResponse:
        """One completion with quota, limiter and bounded retry. Raises the last typed error."""
        t_start = self._perf()
        estimate = estimate_tokens(req)
        attempt = 0
        while True:
            attempt += 1
            state.transport += 1
            if self._quota is not None:
                self._quota.reserve()  # QuotaExhausted propagates: never retried
            if self._limiter is not None:
                self._limiter.acquire(estimate)
            t0 = self._perf()
            try:
                resp = self.adapter.complete(req)
            except QuotaExhausted:
                state.model_s += self._perf() - t0
                raise
            except (RateLimited, ProviderTimeout, ProviderUnavailable) as exc:
                state.model_s += self._perf() - t0
                retry_after = exc.retry_after_s if isinstance(exc, RateLimited) else None
                delay = self._retry.delay(attempt, retry_after, self._rng)
                out_of_attempts = attempt >= self._retry.max_attempts
                out_of_time = (self._perf() - t_start) + delay > self._retry.total_budget_s
                if out_of_attempts or out_of_time:
                    raise
                self._sleep(delay)
                continue
            state.model_s += self._perf() - t0
            if self._limiter is not None:
                self._limiter.settle(estimate, resp.usage.total_tokens)
            return resp

    def _finish(
        self,
        resp: LLMResponse,
        req: LLMRequest,
        trace_id: str,
        cache_status: Literal["hit", "miss", "off", "replay_miss"],
        state: _Stats,
        started: float,
        error: str | None,
        kind: str,
    ) -> LLMResponse:
        trace = CallTrace(
            trace_id=trace_id,
            provider=self.adapter.name,
            model=self.adapter.model,
            cache=cache_status,
            outcome="ok",
            adapter_calls=state.adapter_calls,
            transport_attempts=state.transport,
            prompt_tokens=resp.usage.prompt_tokens,
            completion_tokens=resp.usage.completion_tokens,
            model_ms=state.model_s * 1000,
            total_ms=(self._perf() - started) * 1000,
            scenario_id=req.context.scenario_id,
        )
        self._log_trace(trace)
        return resp.model_copy(update={"trace": trace})

    def _log_trace(self, trace: CallTrace) -> None:
        log.info("llm_call %s", trace.model_dump_json())
        if self._engine is None:
            return
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO llm_calls (trace_id, scenario_id, provider, model,"
                        " cache_status, outcome, error_kind, adapter_calls, transport_attempts,"
                        " prompt_tokens, completion_tokens, model_ms, total_ms)"
                        " VALUES (:t, :s, :p, :m, :c, :o, :e, :a, :ta, :pt, :ct, :mm, :tm)"
                    ),
                    {
                        "t": trace.trace_id,
                        "s": trace.scenario_id,
                        "p": trace.provider,
                        "m": trace.model,
                        "c": trace.cache,
                        "o": trace.outcome,
                        "e": trace.error_kind,
                        "a": trace.adapter_calls,
                        "ta": trace.transport_attempts,
                        "pt": trace.prompt_tokens,
                        "ct": trace.completion_tokens,
                        "mm": round(trace.model_ms),
                        "tm": round(trace.total_ms),
                    },
                )
        except SQLAlchemyError:  # a failed trace write must not change the call's outcome
            log.exception("could not persist llm_calls row for trace %s", trace.trace_id)
