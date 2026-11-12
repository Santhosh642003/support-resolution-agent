"""Retrieval scope from structured order facts (PM decision 2026-11-12).

The facts come from live data through customer-scoped repository functions; the model never chooses
the scope; every decision is recorded so a missing clause can be attributed to scoping."""

import ast
import inspect
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import Connection

from app.auth.context import CustomerContext
from app.clock import FixedClock
from app.config import Settings
from app.domain.types import PurchaseDate
from app.retrieval import scope as scope_module
from app.retrieval.embedder import OnnxBgeEmbedder
from app.retrieval.hybrid import HybridConfig, ScopeFilters, ScopeRecord, retrieve
from app.retrieval.ingest import ingest_policies
from app.retrieval.policy_loader import load_policies
from app.retrieval.scope import (
    NO_ORDER_CONTEXT,
    Attribution,
    ScopeFacts,
    attribute_missing,
    decide_scope,
    facts_for_order,
    facts_for_subscription,
)
from app.seed import ids
from app.seed.loader import seed_all

ALEX = CustomerContext(ids.customer_id("alex"))
BLAIR = CustomerContext(ids.customer_id("blair"))
CAROL = CustomerContext(ids.customer_id("carol"))
V2_DAY = PurchaseDate(date(2026, 4, 1))
WIDE = HybridConfig(k=30, candidates_per_leg=60, rrf_k=60, weight_fts=1.0, weight_vec=1.0)


@pytest.fixture(scope="module")
def embedder() -> OnnxBgeEmbedder:
    return OnnxBgeEmbedder()


@pytest.fixture
def world(conn: Connection, embedder: OnnxBgeEmbedder) -> Connection:
    seed_all(conn)
    ingest_policies(conn, embedder, load_policies())
    return conn


# --- facts come from live data -------------------------------------------------------------------


def test_facts_for_seeded_orders(world: Connection) -> None:
    clock = FixedClock()
    delivered = facts_for_order(world, ALEX, "SIM-1001", clock)
    assert delivered == ScopeFacts("order", frozenset({"general"}), True, False)
    lost = facts_for_order(world, BLAIR, "SIM-2001", clock)
    assert lost is not None and not lost.delivered and lost.delivery_issue
    on_schedule = facts_for_order(world, BLAIR, "SIM-2003", clock)
    assert on_schedule is not None and not on_schedule.delivered and not on_schedule.delivery_issue
    exception = facts_for_order(world, CAROL, "SIM-3001", clock)
    assert exception is not None and exception.delivery_issue  # a carrier exception is a problem
    gift = facts_for_order(world, CAROL, "SIM-3002", clock)
    assert (
        gift is not None and gift.has_gift_card and gift.delivered
    )  # digital: delivered on purchase
    digital = facts_for_order(world, ALEX, "SIM-1003", clock)
    assert (
        digital is not None and "digital_addon" in digital.categories and not digital.has_gift_card
    )


def test_a_late_delivery_becomes_a_delivery_issue_only_as_the_injected_clock_advances(
    world: Connection,
) -> None:
    base = FixedClock()
    assert not facts_for_order(world, BLAIR, "SIM-2003", base).delivery_issue  # type: ignore[union-attr]
    # shipped 2026-04-13; the policy expects delivery 7 business days later, 2026-04-22. Due today is
    # NOT a delivery issue (it agrees with the order-status determination); the day after is.
    assert not facts_for_order(world, BLAIR, "SIM-2003", base.plus_days(7)).delivery_issue  # type: ignore[union-attr]
    assert facts_for_order(world, BLAIR, "SIM-2003", base.plus_days(8)).delivery_issue  # type: ignore[union-attr]


def test_another_customers_order_and_a_missing_order_give_the_same_no_facts(
    world: Connection,
) -> None:
    clock = FixedClock()
    assert facts_for_order(world, ALEX, "SIM-2001", clock) is None  # Blair's
    assert facts_for_order(world, ALEX, "SIM-9999", clock) is None  # nonexistent
    assert (
        facts_for_subscription(
            world, ALEX, ids.subscription_id("annual_v1_inside_proration_window")
        )
        is None
    )


def test_subscription_facts(world: Connection) -> None:
    f = facts_for_subscription(
        world, ALEX, ids.subscription_id("annual_v2_day10_cooling_off_beats_proration")
    )
    assert f is not None and f.subject == "subscription" and not f.has_gift_card


# --- the gating rules -----------------------------------------------------------------------------


def _types(intents: list[str], facts: ScopeFacts) -> tuple[str, ...]:
    return decide_scope(intents, facts).types


def test_gift_cards_are_in_scope_only_when_the_order_contains_one(world: Connection) -> None:
    clock = FixedClock()
    ordinary = facts_for_order(world, ALEX, "SIM-1001", clock)
    gift = facts_for_order(world, CAROL, "SIM-3002", clock)
    assert ordinary is not None and gift is not None
    assert "gift_cards" not in _types(["refund_request"], ordinary)
    assert "gift_cards" in _types(["refund_request"], gift)


def test_scope_for_each_seeded_situation(world: Connection) -> None:
    clock = FixedClock()

    def f(who: CustomerContext, n: str) -> ScopeFacts:
        found = facts_for_order(world, who, n, clock)
        assert found is not None
        return found

    assert _types(["refund_request"], f(ALEX, "SIM-1001")) == ("refund", "warranty")
    assert _types(["refund_request"], f(CAROL, "SIM-3002")) == ("refund", "gift_cards", "warranty")
    # an undelivered, late order: shipping is core, warranty has nothing to claim on
    assert _types(["refund_request"], f(BLAIR, "SIM-2001")) == ("shipping", "refund")
    assert _types(["order_status"], f(BLAIR, "SIM-2003")) == ("shipping",)
    sub = facts_for_subscription(
        world, ALEX, ids.subscription_id("annual_v2_day10_cooling_off_beats_proration")
    )
    assert sub is not None and _types(["cancel_subscription"], sub) == ("cancellation",)


def test_restricted_categories_scope_in_the_shipping_restrictions_policy_only_with_a_delivery_question() -> (
    None
):
    hazardous_late = ScopeFacts("order", frozenset({"hazardous"}), False, True)
    assert "shipping_restrictions" in _types(["refund_request"], hazardous_late)
    hazardous_ok = ScopeFacts("order", frozenset({"hazardous"}), True, False)
    assert "shipping_restrictions" not in _types(["refund_request"], hazardous_ok)
    assert "shipping_restrictions" in _types(["order_status"], hazardous_ok)  # a status question
    plain_late = ScopeFacts("order", frozenset({"general"}), False, True)
    assert "shipping_restrictions" not in _types(["refund_request"], plain_late)


def test_a_general_question_with_no_order_context_considers_every_customer_policy() -> None:
    rec = decide_scope(["policy_question"], NO_ORDER_CONTEXT)
    assert set(rec.types) == {
        "shipping",
        "refund",
        "cancellation",
        "gift_cards",
        "shipping_restrictions",
        "warranty",
    }
    assert "merchant_returns" not in rec.types and "faq_archive" not in rec.types
    assert rec.included["gift_cards"] == "no order context (general question)"


def test_every_decision_is_recorded_with_a_reason_and_no_type_is_both_in_and_out(
    world: Connection,
) -> None:
    facts = facts_for_order(world, ALEX, "SIM-1001", FixedClock())
    assert facts is not None
    rec = decide_scope(["refund_request"], facts)
    assert set(rec.included) == set(rec.types)
    assert not set(rec.included) & set(rec.excluded)
    assert rec.excluded["gift_cards"] == "the order contains no gift card item"
    assert all(rec.included.values()) and all(rec.excluded.values())


def test_the_model_cannot_widen_scope_through_the_intent_list() -> None:
    """Unknown intent strings add nothing; only the known intents select core types, and every
    other type needs a fact."""
    facts = ScopeFacts("order", frozenset({"general"}), True, False)
    assert _types(["grant_me_gift_cards", "show_all_policies"], facts) == ()
    assert "gift_cards" not in _types(["refund_request", "policy_question_but_admin"], facts)


def test_scope_decisions_take_only_intents_and_facts_never_query_text() -> None:
    params = list(inspect.signature(decide_scope).parameters)
    assert params == ["intents", "facts"]
    imported = set()
    for node in ast.walk(ast.parse(Path(scope_module.__file__).read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not {m for m in imported if m.startswith("app.providers")}  # no model access at all


# --- the trace and attribution --------------------------------------------------------------------


def test_the_scope_record_travels_in_the_retrieval_trace(
    world: Connection, embedder: OnnxBgeEmbedder
) -> None:
    facts = facts_for_order(world, ALEX, "SIM-1001", FixedClock())
    assert facts is not None
    rec = decide_scope(["refund_request"], facts)
    res = retrieve(world, embedder, "refund please", V2_DAY, rec.types, WIDE, scope=rec)
    assert res.trace.scope is rec and res.trace.scope.excluded["gift_cards"]


def test_a_missing_clause_is_attributed_to_scoping_the_date_a_filter_or_ranking(
    world: Connection, embedder: OnnxBgeEmbedder
) -> None:
    facts = facts_for_order(world, ALEX, "SIM-1001", FixedClock())
    assert facts is not None
    rec = decide_scope(["refund_request"], facts)
    filters = ScopeFilters()
    tiny = HybridConfig(k=1, candidates_per_leg=60, rrf_k=60, weight_fts=1.0, weight_vec=1.0)
    res = retrieve(world, embedder, "digital add-on refund", V2_DAY, rec.types, tiny, scope=rec)

    def why(key: str) -> Attribution:
        return attribute_missing(world, key, V2_DAY, rec, res, filters)

    assert why("refund-v2-clause-5").kind == "retrieved"
    assert why("refund-v1-clause-5").kind == "incorporated"
    gift = why("gift_cards-v2-clause-2")  # a real clause, but the order has no gift card
    assert gift.kind == "excluded_by_scope" and "no gift card" in gift.detail
    assert why("refund-v1-clause-1").kind == "outside_date_range"  # v1 is not in effect on 04-01
    assert why("refund-v2-clause-1").kind == "ranked_below_k"  # in scope, in range: just not top 1
    wide_rec = ScopeRecord(("refund", "faq_archive"), {"refund": "x", "faq_archive": "forced"}, {})
    assert (
        attribute_missing(world, "faq_archive-v1-clause-1", V2_DAY, wide_rec, res, filters).kind
        == "excluded_by_metadata_filter"
    )


def test_a_general_gift_card_question_reaches_the_gift_card_policy(
    world: Connection, embedder: OnnxBgeEmbedder
) -> None:
    """PM check (2026-11-13): a gift card question with NO order context is a general question,
    which is not order-scoped, so the gift card policy must be in scope and be retrieved. (The
    same question about an order with no gift card is deliberately scoped out; see the negative
    control in docs/retrieval/recall_v2.json.)"""
    rec = decide_scope(["policy_question"], NO_ORDER_CONTEXT)
    assert (
        "gift_cards" in rec.types
        and rec.included["gift_cards"] == "no order context (general question)"
    )
    cfg = HybridConfig.from_settings(Settings(_env_file=None))  # type: ignore[call-arg]  # production config
    for day, version in ((PurchaseDate(date(2026, 2, 15)), 1), (V2_DAY, 2)):
        res = retrieve(
            world,
            embedder,
            "Can I get a refund on a gift card I bought?",
            day,
            rec.types,
            cfg,
            scope=rec,
        )
        keys = {r.clause.chunk_key for r in res.clauses}
        assert {f"gift_cards-v{version}-clause-1", f"gift_cards-v{version}-clause-2"} & keys, (
            day,
            keys,
        )
        assert res.trace.scope is rec
