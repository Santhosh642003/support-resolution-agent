"""Shared builders for workflow tests. Everything runs on fakes: zero network."""

import json
from pathlib import Path
from typing import Any

from app.clock import Clock, FixedClock
from app.config import Settings
from app.providers.adapters.fake import FakeAdapter
from app.providers.base import LLMProvider, LLMRequest, LLMResponse
from app.providers.cache import CacheMode
from app.providers.client import LLMClient
from app.providers.guard import PromptGuard
from app.retrieval.embedder import Embedder
from app.retrieval.hybrid import HybridConfig
from app.workflow.order_status import OrderStatusWorkflow
from tests.providers.helpers import FakeTime, fixed_rng, reply

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "llm"


class InspectableFake(FakeAdapter):
    """The committed-fixture adapter, plus a record of every request it received."""

    def __init__(self, directory: Path = FIXTURES) -> None:
        super().__init__(directory)
        self.requests: list[LLMRequest] = []

    def complete(self, req: LLMRequest) -> LLMResponse:
        self.requests.append(req)
        return super().complete(req)


def hybrid_config() -> HybridConfig:
    settings: Any = Settings
    return HybridConfig.from_settings(settings(_env_file=None))


def build_workflow(
    adapter: LLMProvider,
    embedder: Embedder,
    *,
    guard: PromptGuard | None = None,
    clock: Clock | None = None,
    client: LLMClient | None = None,
    **kw: Any,
) -> OrderStatusWorkflow:
    t = FakeTime()
    llm = client or LLMClient(
        adapter, cache_mode=CacheMode.OFF, sleep=t.sleep, rng=fixed_rng(1.0), perf=t.now
    )
    return OrderStatusWorkflow(
        llm,
        embedder,
        clock or FixedClock(),
        hybrid_config(),
        guard=guard,
        explanation_max_tokens=1500,
        **kw,
    )


def extraction_reply(
    order_number: str | None = "SIM-2001",
    intent: str = "order_status",
    claims: list[str] | None = None,
) -> LLMResponse:
    return reply(
        json.dumps(
            {
                "intent": intent,
                "order_number": order_number,
                "confidence": 0.95,
                "customer_claims": claims or [],
            }
        ),
        total=400,
    )


def explanation_reply(outcome: str, keys: list[str], text: str) -> LLMResponse:
    return reply(
        json.dumps({"outcome": outcome, "cited_clause_keys": keys, "explanation_text": text}),
        total=800,
    )
