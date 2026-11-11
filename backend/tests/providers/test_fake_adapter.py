"""FakeAdapter: scenario+step fixtures, staleness warns loudly but does not fail, no invention."""

import json
import logging
from pathlib import Path

import pytest

from app.providers.adapters import fake
from app.providers.adapters.fake import FakeAdapter, RecordingAdapter, StaleFixtureWarning
from app.providers.base import (
    CallContext,
    FixtureMissing,
    LLMRequest,
    Message,
    QuotaExhausted,
    RateLimited,
    prompt_hash,
)
from tests.providers.helpers import ScriptedAdapter, reply, write_fixture


def req(text: str = "hi", scenario: str | None = "s1", step: int = 0) -> LLMRequest:
    return LLMRequest(
        messages=(Message(role="user", content=text),),
        context=CallContext(scenario_id=scenario, step=step),
    )


def test_returns_the_fixture_for_the_scenario_and_step(tmp_path: Path) -> None:
    write_fixture(
        tmp_path,
        "s1",
        [
            {"step": 0, "prompt_hash": prompt_hash(req()), "response": {"text": "first"}},
            {"step": 1, "response": {"text": "second", "usage": {"total_tokens": 5}}},
        ],
    )
    a = FakeAdapter(tmp_path)
    assert a.complete(req(step=0)).text == "first"
    r = a.complete(req("anything else", step=1))  # no stored hash: nothing to compare
    assert r.text == "second" and r.usage.total_tokens == 5


def test_missing_scenario_or_step_raises_and_never_invents_output(tmp_path: Path) -> None:
    write_fixture(tmp_path, "s1", [{"step": 0, "response": {"text": "x"}}])
    a = FakeAdapter(tmp_path)
    with pytest.raises(FixtureMissing, match="no step 3"):
        a.complete(req(step=3))
    with pytest.raises(FixtureMissing, match="no fixture file"):
        a.complete(req(scenario="nope"))
    with pytest.raises(FixtureMissing, match="scenario_id"):
        a.complete(req(scenario=None))


def test_a_stale_prompt_hash_warns_loudly_counts_and_still_returns_the_fixture(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    write_fixture(
        tmp_path,
        "s1",
        [{"step": 0, "prompt_hash": "0" * 64, "response": {"text": "still served"}}],
    )
    before = len(fake.STALENESS_EVENTS)
    with caplog.at_level(logging.WARNING), pytest.warns(StaleFixtureWarning, match="s1.*step 0"):
        r = FakeAdapter(tmp_path).complete(req())
    assert r.text == "still served"  # warns, does not fail
    assert len(fake.STALENESS_EVENTS) == before + 1
    assert "STALE FIXTURE" in caplog.text and "re-record" in caplog.text
    del fake.STALENESS_EVENTS[before:]  # these were deliberate; keep the suite summary clean


def test_fixtures_can_script_failures(tmp_path: Path) -> None:
    write_fixture(
        tmp_path,
        "s1",
        [
            {"step": 0, "error": {"kind": "QuotaExhausted", "message": "daily limit"}},
            {"step": 1, "error": {"kind": "RateLimited", "retry_after_s": 4.5}},
        ],
    )
    a = FakeAdapter(tmp_path)
    with pytest.raises(QuotaExhausted, match="daily limit"):
        a.complete(req(step=0))
    with pytest.raises(RateLimited) as e:
        a.complete(req(step=1))
    assert e.value.retry_after_s == 4.5


def test_recording_adapter_writes_fixtures_that_the_fake_adapter_then_replays(
    tmp_path: Path,
) -> None:
    inner = ScriptedAdapter([reply("recorded one"), reply("recorded two")])
    rec = RecordingAdapter(inner, tmp_path)
    rec.complete(req("q1", "rec", 0))
    rec.complete(req("q2", "rec", 1))
    doc = json.loads((tmp_path / "rec.json").read_text(encoding="utf-8"))
    assert [s["step"] for s in doc["steps"]] == [0, 1]
    assert doc["steps"][0]["prompt_hash"] == prompt_hash(req("q1", "rec", 0))
    replay = FakeAdapter(tmp_path)
    assert replay.complete(req("q1", "rec", 0)).text == "recorded one"  # hash matches: silent
    assert replay.complete(req("q2", "rec", 1)).text == "recorded two"
