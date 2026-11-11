"""Test doubles for the provider layer. No network anywhere."""

import json
import random
from pathlib import Path
from typing import Any

from app.providers.base import (
    AdapterCapabilities,
    LLMError,
    LLMRequest,
    LLMResponse,
    ToolCall,
    Usage,
)


def reply(
    text: str = "ok", *, total: int = 10, tool_calls: tuple[ToolCall, ...] = ()
) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=tool_calls,
        usage=Usage(prompt_tokens=total - 2, completion_tokens=2, total_tokens=total),
        finish_reason="stop",
        model="scripted-model",
    )


class ScriptedAdapter:
    """Returns / raises a scripted sequence, then fails loudly if asked for more."""

    name = "scripted"
    model = "scripted-model"
    capabilities = AdapterCapabilities(json_mode=True, json_schema=True, tool_calling=True)

    def __init__(self, script: list[LLMResponse | LLMError]) -> None:
        self._script = list(script)
        self.requests: list[LLMRequest] = []

    @property
    def calls(self) -> int:
        return len(self.requests)

    def complete(self, req: LLMRequest) -> LLMResponse:
        self.requests.append(req)
        if not self._script:
            raise AssertionError("ScriptedAdapter was called more times than scripted")
        item = self._script.pop(0)
        if isinstance(item, LLMError):
            raise item
        return item


class FakeTime:
    """A controllable monotonic clock plus a sleep that advances it instead of waiting."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


def fixed_rng(value: float = 1.0) -> random.Random:
    """A Random whose uniform(0, x) always returns value*x, so backoff is exactly computable."""

    class _R(random.Random):
        def uniform(self, a: float, b: float) -> float:
            return a + (b - a) * value

    return _R()


def write_fixture(directory: Path, scenario_id: str, steps: list[dict[str, Any]]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{scenario_id}.json").write_text(
        json.dumps({"scenario_id": scenario_id, "steps": steps}), encoding="utf-8"
    )
