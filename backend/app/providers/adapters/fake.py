"""Deterministic, network-free adapter for tests, CI and evaluation.

Fixtures are keyed by SCENARIO ID + STEP INDEX (PM decision 2026-11-10), one JSON file per
scenario in the fixtures directory:

    {"scenario_id": "refund_basic",
     "steps": [{"step": 0, "prompt_hash": "<sha256>",
                "response": {"text": "...", "tool_calls": [], "usage": {}}},
               {"step": 1, "error": {"kind": "QuotaExhausted", "message": "..."}}]}

The stored prompt hash is a STALENESS CHECK ONLY: on mismatch the adapter logs a loud warning
naming the scenario and step, records it (counted in the pytest summary) and STILL returns the
fixture. A missing scenario or step raises FixtureMissing; output is never invented.

RecordingAdapter (RECORD=1, run manually against a real provider) writes these files.
"""

import json
import logging
import warnings
from pathlib import Path
from typing import Any

from app.providers.base import (
    AdapterCapabilities,
    FixtureMissing,
    LLMError,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ProviderTimeout,
    ProviderUnavailable,
    QuotaExhausted,
    RateLimited,
    prompt_hash,
)

log = logging.getLogger(__name__)

# Every staleness warning raised in this process, for the test summary.
STALENESS_EVENTS: list[str] = []


class StaleFixtureWarning(UserWarning):
    pass


_ERRORS: dict[str, type[LLMError]] = {
    "QuotaExhausted": QuotaExhausted,
    "ProviderTimeout": ProviderTimeout,
    "ProviderUnavailable": ProviderUnavailable,
}


def _raise_fixture_error(spec: dict[str, Any]) -> None:
    kind, message = spec["kind"], spec.get("message", spec["kind"])
    if kind == "RateLimited":
        raise RateLimited(message, retry_after_s=spec.get("retry_after_s"), scope="minute")
    if kind not in _ERRORS:
        raise FixtureMissing(f"fixture names an unknown error kind {kind!r}")
    raise _ERRORS[kind](message)


class FakeAdapter:
    name = "fake"
    capabilities = AdapterCapabilities(json_mode=True, json_schema=True, tool_calling=True)

    def __init__(self, fixtures_dir: str | Path, model: str = "fake-model") -> None:
        self.model = model
        self._dir = Path(fixtures_dir)
        self._cache: dict[str, dict[str, Any]] = {}
        self.calls = 0  # how many times complete() was invoked (tests assert zero-call replays)

    def _scenario(self, scenario_id: str) -> dict[str, Any]:
        if scenario_id not in self._cache:
            path = self._dir / f"{scenario_id}.json"
            if not path.is_file():
                raise FixtureMissing(f"no fixture file for scenario {scenario_id!r} ({path})")
            self._cache[scenario_id] = json.loads(path.read_text(encoding="utf-8"))
        return self._cache[scenario_id]

    def complete(self, req: LLMRequest) -> LLMResponse:
        self.calls += 1
        sid, step = req.context.scenario_id, req.context.step
        if sid is None:
            raise FixtureMissing("FakeAdapter needs LLMRequest.context.scenario_id")
        entry = next((s for s in self._scenario(sid)["steps"] if s["step"] == step), None)
        if entry is None:
            raise FixtureMissing(f"scenario {sid!r} has no step {step}")
        stored = entry.get("prompt_hash")
        if stored is not None and stored != prompt_hash(req):
            msg = (
                f"STALE FIXTURE: scenario {sid!r} step {step}: the prompt no longer matches the "
                "recorded one. Returning the fixture anyway; re-record it."
            )
            log.warning(msg)
            STALENESS_EVENTS.append(msg)
            warnings.warn(msg, StaleFixtureWarning, stacklevel=2)
        if "error" in entry:
            _raise_fixture_error(entry["error"])
        return LLMResponse.model_validate({**entry["response"], "model": self.model})


class RecordingAdapter:
    """Wraps a real adapter and writes each call as a fixture (scenario + step). Manual use only."""

    def __init__(self, inner: LLMProvider, fixtures_dir: str | Path) -> None:
        self._inner = inner
        self._dir = Path(fixtures_dir)
        self.name = inner.name
        self.model = inner.model
        self.capabilities = inner.capabilities

    def complete(self, req: LLMRequest) -> LLMResponse:
        resp = self._inner.complete(req)
        sid = req.context.scenario_id
        if sid is None:
            raise FixtureMissing("RECORD=1 needs LLMRequest.context.scenario_id")
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"{sid}.json"
        doc: dict[str, Any] = (
            json.loads(path.read_text(encoding="utf-8"))
            if path.is_file()
            else {"scenario_id": sid, "steps": []}
        )
        doc["steps"] = [s for s in doc["steps"] if s["step"] != req.context.step]
        doc["steps"].append(
            {
                "step": req.context.step,
                "prompt_hash": prompt_hash(req),
                "response": resp.model_dump(mode="json", exclude={"trace", "model", "latency_ms"}),
            }
        )
        doc["steps"].sort(key=lambda s: s["step"])
        path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return resp
