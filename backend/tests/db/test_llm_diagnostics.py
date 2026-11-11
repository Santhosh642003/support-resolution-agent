"""The redacted error body and the fallback count are persisted in llm_calls (migration 0007)."""

from typing import Literal

import pytest
from pydantic import BaseModel
from sqlalchemy import Engine, text

from app.providers.base import ProviderRejected
from tests.db.test_llm_layer import make_client, scripted, unique_request
from tests.providers.helpers import reply


class Intent(BaseModel):
    intent: Literal["a", "b"]


def _rows(engine: Engine, provider: str) -> list[tuple[object, ...]]:
    with engine.connect() as c:
        return [
            tuple(r)
            for r in c.execute(
                text(
                    "SELECT outcome, error_kind, error_detail, native_fallbacks FROM llm_calls"
                    " WHERE provider = :p ORDER BY id"
                ),
                {"p": provider},
            )
        ]


def test_an_error_trace_stores_the_redacted_provider_body(engine: Engine) -> None:
    err = ProviderRejected("rejected (400)", 400)
    err.detail = '{"error": {"message": "bad schema for org_01abc123def key gsk_abcdefghijklmnop"}}'
    a = scripted([err])
    with pytest.raises(ProviderRejected):
        make_client(engine, a).complete(unique_request())
    ((outcome, kind, detail, fallbacks),) = _rows(engine, a.name)
    assert (outcome, kind, fallbacks) == ("error", "ProviderRejected", 0)
    assert isinstance(detail, str) and "bad schema" in detail
    assert "org_01abc123def" not in detail and "gsk_abcdefghijklmnop" not in detail


def test_a_fallback_is_counted_in_the_persisted_trace(engine: Engine) -> None:
    err = ProviderRejected("rejected (400)", 400)
    err.detail = "json_validate_failed"
    a = scripted([err, reply('{"intent": "a"}')])
    make_client(engine, a, native_json="json_schema").complete_structured(unique_request(), Intent)
    ((outcome, kind, detail, fallbacks),) = _rows(engine, a.name)
    assert (outcome, kind, detail, fallbacks) == ("ok", None, None, 1)
