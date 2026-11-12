"""POST /chat: session identity only, determination values only, trace persisted for every turn."""

from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine

from app.api.deps import get_conn
from app.auth.sessions import SESSION_COOKIE, create_session
from app.clock import FixedClock
from app.main import create_app
from app.providers.base import QuotaExhausted
from app.providers.guard import FakePromptGuard
from app.retrieval.embedder import OnnxBgeEmbedder
from app.retrieval.ingest import ingest_policies
from app.retrieval.policy_loader import load_policies
from app.seed import ids
from app.seed.loader import seed_all
from tests.db.scoped import Scope, count_rows, fetch_rows
from tests.providers.helpers import ScriptedAdapter
from tests.workflow.helpers import build_workflow, explanation_reply, extraction_reply

C1, C2 = "shipping-v1-clause-1", "shipping-v1-clause-2"
GOOD = explanation_reply(
    "presumed_lost", [C2], "It shipped on 2026-03-11 and is presumed lost since 2026-03-25."
)
MSG = "Where is my order SIM-2001?"


@pytest.fixture(scope="module")
def embedder() -> OnnxBgeEmbedder:
    return OnnxBgeEmbedder()


class Env:
    def __init__(self, conn: Connection, engine: Engine, embedder: OnnxBgeEmbedder) -> None:
        seed_all(conn)
        ingest_policies(conn, embedder, load_policies())
        self.conn, self.engine, self.embedder = conn, engine, embedder
        self.sessions = {
            n: create_session(conn, ids.customer_id(n)) for n in ("alex", "blair", "carol")
        }

    def client(
        self, who: str | None, script: list[Any] | None = None, guard: Any = None
    ) -> TestClient:
        adapter = ScriptedAdapter(
            script if script is not None else [extraction_reply("SIM-2001"), GOOD]
        )
        wf = build_workflow(adapter, self.embedder, guard=guard or FakePromptGuard())
        app = create_app(engine=self.engine, clock=FixedClock(), workflow=wf)
        conn = self.conn

        def same() -> Iterator[Connection]:
            yield conn  # the test's own transaction: sees the seed, rolls back at the end

        app.dependency_overrides[get_conn] = same
        c = TestClient(app)
        if who:
            c.cookies.set(SESSION_COOKIE, str(self.sessions[who]))
        return c


@pytest.fixture
def env(conn: Connection, engine: Engine, embedder: OnnxBgeEmbedder) -> Env:
    return Env(conn, engine, embedder)


def keys_of(node: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(node, dict):
        for k, v in node.items():
            out.add(str(k))
            out |= keys_of(v)
    elif isinstance(node, list):
        for v in node:
            out |= keys_of(v)
    return out


def post(c: TestClient, message: str = MSG, **extra: Any) -> Any:
    return c.post("/chat", json={"message": message, **extra})


def test_no_session_is_401(env: Env) -> None:
    r = post(env.client(None))
    assert r.status_code == 401 and r.json() == {"detail": "authentication required"}


def test_a_turn_returns_the_determinations_values_citations_and_the_explanation(env: Env) -> None:
    r = post(env.client("blair"))
    assert r.status_code == 200
    b = r.json()
    assert b["state"] == "ANSWERED" and b["simulated"] is True and b["degraded"] is False
    d = b["determination"]
    assert d["outcome"] == "presumed_lost" and d["authorizes_action"] is False
    assert d["decisive_clause_keys"] == [C2] and d["supporting_clause_keys"] == [C1]
    assert d["facts"]["presumed_lost_on"] == "2026-03-25" and "PRESUMED_LOST" in d["reason_codes"]
    assert [(c["clause_key"], c["role"]) for c in b["citations"]] == [
        (C1, "supporting"),
        (C2, "decisive"),
    ]
    assert b["reply"].startswith("It shipped on 2026-03-11 and is presumed lost since 2026-03-25.")
    assert b["as_of"] == "2026-04-15" and b["trace_id"] and UUID(b["conversation_id"])
    # no field exposes the model's own outcome or the Prompt Guard verdict
    assert not {"model_outcome", "prompt_guard", "model_cited"} & keys_of(b)


def test_nothing_in_a_request_path_body_or_response_names_a_customer(env: Env) -> None:
    schema = env.client(None).app.openapi()  # type: ignore[attr-defined]
    op = schema["paths"]["/chat"]["post"]
    assert not any("customer" in p["name"].lower() for p in op.get("parameters", []))
    body = schema["components"]["schemas"]["ChatRequest"]
    assert body["additionalProperties"] is False and set(body["properties"]) == {
        "message",
        "conversation_id",
    }
    chat_schemas = ("ChatRequest", "ChatResponse", "CitationOut", "DeterminationOut")
    names = {k.lower() for n in chat_schemas for k in keys_of(schema["components"]["schemas"][n])}
    assert not any("customer" in n for n in names)
    r = post(env.client("blair"))
    assert str(ids.customer_id("blair")) not in r.text and "customer" not in {
        k.lower() for k in keys_of(r.json())
    }


def test_a_smuggled_customer_id_is_rejected_not_ignored(env: Env) -> None:
    c = env.client("alex")
    assert post(c, customer_id=str(ids.customer_id("blair"))).status_code == 422
    assert post(c, MSG, conversation_id=None, owner="x").status_code == 422
    # a query parameter or header changes nothing about whose order this is
    r = c.post(
        "/chat?customer_id=" + str(ids.customer_id("blair")),
        json={"message": MSG},
        headers={"X-Customer-Id": "x"},
    )
    assert r.status_code == 200 and r.json()["determination"] is None  # still alex: not his order


def test_another_customers_order_and_a_nonexistent_one_get_the_same_reply(env: Env) -> None:
    foreign = post(env.client("alex", [extraction_reply("SIM-2001")]))
    missing = post(
        env.client("alex", [extraction_reply("SIM-9999")]), "Where is my order SIM-9999?"
    )
    assert foreign.status_code == missing.status_code == 200
    f, m = foreign.json(), missing.json()
    assert f["reply"].split("\n\n(")[0] == m["reply"].split("\n\n(")[0]
    assert f["determination"] is None and f["citations"] == [] and m["determination"] is None


def test_another_customers_conversation_id_is_the_same_404_as_an_unknown_one(env: Env) -> None:
    mine = post(env.client("blair")).json()["conversation_id"]
    stolen = post(env.client("alex", [extraction_reply("SIM-1001")]), conversation_id=mine)
    unknown = post(env.client("alex", [extraction_reply("SIM-1001")]), conversation_id=str(uuid4()))
    assert stolen.status_code == unknown.status_code == 404
    assert stolen.content == unknown.content


def test_a_conversation_continues_and_the_turn_number_advances(env: Env) -> None:
    c = env.client(
        "blair", [extraction_reply("SIM-2001"), GOOD, extraction_reply("SIM-2001"), GOOD]
    )
    a = post(c).json()
    b = post(c, conversation_id=a["conversation_id"]).json()
    assert (a["turn_no"], b["turn_no"]) == (1, 2) and a["conversation_id"] == b["conversation_id"]


def test_a_fail_closed_turn_shows_the_determination_never_the_models_words_and_is_traced(
    env: Env,
) -> None:
    wrong = explanation_reply("delivered", [C1], "Wonderful news, it was delivered on 2026-03-18!")
    r = post(env.client("blair", [extraction_reply("SIM-2001"), wrong]))
    b = r.json()
    assert (
        r.status_code == 200
        and b["state"] == "HANDED_OFF"
        and b["handoff_reason"] == "EXPLANATION_MISMATCH"
    )
    assert (
        b["determination"]["outcome"] == "presumed_lost"
    )  # the determination's value, not the model's
    assert (
        "Wonderful news" not in r.text
        and "2026-03-18" not in r.text
        and "delivered" not in b["reply"].lower().split("treated")[0]
    )
    cid = UUID(b["conversation_id"])
    (row,) = fetch_rows(
        env.conn,
        "turn_traces",
        Scope.ids("conversation_id", cid),
        "final_state, model_calls, trace",
    )
    assert (
        row.final_state == "HANDED_OFF"
        and row.model_calls == 2
        and row.trace["trace_id"] == b["trace_id"]
    )
    assert row.trace["explanation"]["accepted"] is False


def test_a_prompt_guard_hit_is_traced_and_reviewed_but_the_turn_is_still_answered(env: Env) -> None:
    c = env.client("blair", guard=FakePromptGuard({MSG: 0.99}))
    r = post(c)
    b = r.json()
    assert (
        r.status_code == 200
        and b["state"] == "ANSWERED"
        and b["determination"]["outcome"] == "presumed_lost"
    )
    assert (
        "0.99" not in r.text and "guard" not in r.text.lower()
    )  # the verdict is not exposed to the caller
    cid = UUID(b["conversation_id"])
    (row,) = fetch_rows(env.conn, "turn_traces", Scope.ids("conversation_id", cid), "trace")
    assert row.trace["prompt_guard"]["flagged"] is True and row.trace["review_requested"] is True
    kinds = [
        h.kind for h in fetch_rows(env.conn, "handoffs", Scope.ids("conversation_id", cid), "kind")
    ]
    assert kinds == ["review"]


def test_a_model_outage_is_a_failed_turn_with_a_persisted_trace(env: Env) -> None:
    r = post(env.client("blair", [QuotaExhausted("daily")]))
    b = r.json()
    assert r.status_code == 200 and b["state"] == "FAILED" and b["determination"] is None
    cid = UUID(b["conversation_id"])
    (row,) = fetch_rows(
        env.conn, "turn_traces", Scope.ids("conversation_id", cid), "final_state, trace"
    )
    assert row.final_state == "FAILED" and row.trace["extraction"]["error"] == "QuotaExhausted"
    assert count_rows(env.conn, "decisions", Scope.ids("conversation_id", cid)) == 0


@pytest.mark.parametrize("message", ["", "x" * 2001])
def test_message_length_is_validated(env: Env, message: str) -> None:
    assert post(env.client("blair"), message).status_code == 422


def test_get_is_not_allowed_on_chat(env: Env) -> None:
    assert env.client("blair").get("/chat").status_code == 405
