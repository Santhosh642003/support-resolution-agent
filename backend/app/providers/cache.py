"""Response cache in PostgreSQL.

Modes: read_write (default), replay_only (a miss raises CacheMiss and NO call is made, so an
evaluation rerun costs zero calls and is identical), off. Entries are immutable snapshots of one
sampling (migration 0005). Errors and invalid structured output are never stored (the client
only calls `put` with a validated success). Hits and misses are recorded in llm_calls by the
client and shown in the call trace.

Honesty note: for reasoning models the output is not guaranteed reproducible at temperature 0,
so a cache is a *replay store of one sampling*, not a reproducible computation. See SPEC 4.0.
"""

import json
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import Engine, text

from app.providers.base import LLMRequest, LLMResponse


class CacheMode(StrEnum):
    READ_WRITE = "read_write"
    REPLAY_ONLY = "replay_only"
    OFF = "off"


@dataclass(frozen=True)
class CacheEntry:
    response: LLMResponse
    adapter_calls: int


class LLMCache:
    def __init__(self, engine: Engine, mode: CacheMode = CacheMode.READ_WRITE) -> None:
        self._engine = engine
        self.mode = mode

    def get(self, key: str) -> CacheEntry | None:
        if self.mode is CacheMode.OFF:
            return None
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT response, adapter_calls FROM llm_cache WHERE cache_key = :k"),
                {"k": key},
            ).one_or_none()
        if row is None:
            return None
        return CacheEntry(LLMResponse.model_validate(row.response), int(row.adapter_calls))

    def put(
        self,
        key: str,
        provider: str,
        model: str,
        kind: str,
        req: LLMRequest,
        response: LLMResponse,
        adapter_calls: int,
    ) -> bool:
        """Store a validated success. Returns False if an entry already exists (first wins)."""
        if self.mode is not CacheMode.READ_WRITE:
            return False
        stored = response.model_copy(update={"trace": None})  # traces are per-call, not cached
        with self._engine.begin() as conn:
            res = conn.execute(
                text(
                    "INSERT INTO llm_cache (cache_key, provider, model, kind,"
                    " prompt_template_version, tool_schema_version, response, adapter_calls)"
                    " VALUES (:k, :p, :m, :kind, :ptv, :tsv, CAST(:r AS jsonb), :n)"
                    " ON CONFLICT (cache_key) DO NOTHING"
                ),
                {
                    "k": key,
                    "p": provider,
                    "m": model,
                    "kind": kind,
                    "ptv": req.prompt_template_version,
                    "tsv": req.tool_schema_version,
                    "r": json.dumps(stored.model_dump(mode="json")),
                    "n": adapter_calls,
                },
            )
        return res.rowcount == 1
