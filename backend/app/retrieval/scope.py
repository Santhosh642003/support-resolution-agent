"""Retrieval scope from STRUCTURED FACTS about the order or subscription (PM decision 2026-11-12).

The retrievable candidate set is narrowed by facts read from live order and subscription data,
not by query text. Examples: the gift card policy is in scope only when the order contains a
gift card; shipping restrictions only for a restricted-category item with a delivery problem; the
warranty policy only once the order is delivered.

TWO CONSTRAINTS, enforced by construction:
  * The facts come from the database (through customer-scoped repository functions, so another
    customer's order yields no facts at all), never from the model and never from the query text.
    The model contributes only the intent enum, which selects the CORE policy types; every
    narrowing beyond that is a gate on a fact. The model does not choose which policies are in
    scope, for the same reason it does not choose citations (SPEC 10.4).
  * Every decision is recorded (`ScopeRecord`: policy type -> reason, for included and excluded
    types) and carried in the retrieval trace, so a missing clause can be attributed to SCOPING
    rather than to ranking (`attribute_missing`).

The facts here are only what the current data model holds. Payment method is not modeled, so
"paid with a gift card" is not a fact; "contains a gift card item" is.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import Connection, text

from app.auth.context import CustomerContext
from app.clock import Clock
from app.domain.rules.shipping import expected_delivery_date, presumed_lost_date
from app.domain.types import PurchaseDate
from app.repositories import state as repo
from app.retrieval.hybrid import (
    RELATED_TYPES,
    RetrievalResult,
    ScopeFilters,
    ScopeRecord,
    _core_types,
)

RESTRICTED_CATEGORIES = frozenset({"hazardous", "perishable", "oversize", "age_restricted"})
_ORDER = (
    "shipping",
    "refund",
    "cancellation",
    "gift_cards",
    "shipping_restrictions",
    "warranty",
    "merchant_returns",
    "faq_archive",
)


@dataclass(frozen=True)
class ScopeFacts:
    """Structured facts about the subject of a request. All derived from live data."""

    subject: Literal["order", "subscription", "none"] = "none"
    categories: frozenset[str] = frozenset()  # product categories present on the order
    delivered: bool = False
    delivery_issue: bool = False  # undelivered and (late, presumed lost, or a carrier exception)

    @property
    def has_gift_card(self) -> bool:
        return "gift_card" in self.categories

    @property
    def has_restricted_item(self) -> bool:
        return bool(self.categories & RESTRICTED_CATEGORIES)


NO_ORDER_CONTEXT = ScopeFacts("none")


def _on(ts: datetime | None) -> date | None:
    return None if ts is None else ts.astimezone(UTC).date()


def scope_facts_from_state(
    categories: frozenset[str],
    order_status: str,
    carrier_status: str | None,
    shipped_on: date | None,
    delivered_on: date | None,
    has_shipment: bool,
    today: date,
) -> ScopeFacts:
    """The scope facts from already-fetched order state (pure). `delivery_issue` agrees exactly
    with the order-status determination: undelivered AND (past the expected date, presumed lost,
    or a carrier exception/loss). 'Due today' is NOT a delivery issue."""
    delivered = delivered_on is not None
    issue = False
    if has_shipment and not delivered:
        late = shipped_on is not None and today > expected_delivery_date(shipped_on)
        lost = shipped_on is not None and today >= presumed_lost_date(shipped_on)
        issue = carrier_status in ("exception", "lost") or late or lost
    elif not has_shipment and order_status == "delivered":
        delivered = True  # a digital order with no shipment is delivered on purchase
    return ScopeFacts("order", categories, delivered, issue)


def facts_for_order(
    conn: Connection, ctx: CustomerContext, order_number: str, clock: Clock
) -> ScopeFacts | None:
    """Facts about one of THIS customer's orders, or None (the same None for a missing order and
    for someone else's order). The delivery facts use the injected clock."""
    order = repo.get_order(conn, ctx, order_number)
    if order is None:
        return None
    categories = repo.categories_of_order(conn, ctx, order.id)
    ship = repo.shipment_of(conn, ctx, order.id)
    return scope_facts_from_state(
        frozenset(categories),
        order.status,
        None if ship is None else ship.status,
        None if ship is None else _on(ship.shipped_at),
        None if ship is None else _on(ship.delivered_at),
        ship is not None,
        clock.today(),
    )


def facts_for_subscription(
    conn: Connection, ctx: CustomerContext, subscription_id: UUID
) -> ScopeFacts | None:
    row = repo.get_subscription(conn, ctx, subscription_id)
    if row is None:
        return None
    return ScopeFacts("subscription", frozenset({"plan"}), True, False)


def decide_scope(intents: Sequence[str], facts: ScopeFacts) -> ScopeRecord:
    """The policy types in scope, with a reason for every type considered."""
    core = _core_types(intents, facts.delivery_issue)
    included: dict[str, str] = {t: f"core policy for intent {sorted(intents)}" for t in core}
    excluded: dict[str, str] = {}
    candidates = {r for t in core for r in RELATED_TYPES.get(t, ())} - core

    def gate(policy_type: str, ok: bool, yes: str, no: str) -> None:
        (included if ok else excluded)[policy_type] = yes if ok else no

    none = facts.subject == "none"
    for t in sorted(candidates):
        if t == "gift_cards":
            gate(
                t,
                none or facts.has_gift_card,
                "no order context (general question)" if none else "the order contains a gift card",
                "the order contains no gift card item",
            )
        elif t == "shipping_restrictions":
            relevant = facts.has_restricted_item and (
                facts.delivery_issue or "order_status" in intents
            )
            gate(
                t,
                none or relevant,
                "no order context (general question)"
                if none
                else "restricted-category item with a delivery question",
                "no restricted-category item with a delivery problem on this order",
            )
        elif t == "warranty":
            gate(
                t,
                none or facts.delivered,
                "no order context (general question)" if none else "the order is delivered",
                "the order is not delivered, so there is nothing to claim a defect on",
            )
    types = tuple(t for t in _ORDER if t in included)
    return ScopeRecord(types, included, excluded)


# --- attribution: why is a clause missing? ----------------------------------------------------


@dataclass(frozen=True)
class Attribution:
    kind: Literal[
        "retrieved",
        "incorporated",
        "outside_date_range",
        "excluded_by_scope",
        "excluded_by_metadata_filter",
        "ranked_below_k",
    ]
    detail: str = ""


def attribute_missing(
    conn: Connection,
    chunk_key: str,
    purchase_date: PurchaseDate,
    scope: ScopeRecord,
    result: RetrievalResult,
    filters: ScopeFilters,
) -> Attribution:
    """Why a clause that was needed is (not) in the result: scoping, the date filter, a metadata
    filter, or plain ranking. Checked in that order, which is the order they are applied."""
    if chunk_key in {r.clause.chunk_key for r in result.clauses}:
        return Attribution("retrieved")
    if chunk_key in {r.clause.chunk_key for r in result.incorporated}:
        return Attribution("incorporated")
    row = conn.execute(
        text(
            "SELECT c.policy_type, c.validity @> CAST(:d AS date) AS in_range, d.audience, d.status"
            " FROM policy_chunks c JOIN policy_documents d ON d.id = c.document_id"
            " WHERE c.chunk_key = :k"
        ),
        {"d": purchase_date, "k": chunk_key},
    ).one()
    if not row.in_range:
        return Attribution("outside_date_range", f"{chunk_key} is not in effect on {purchase_date}")
    if row.policy_type not in scope.types:
        why = scope.excluded.get(row.policy_type, "not a candidate type for this intent")
        return Attribution("excluded_by_scope", f"{row.policy_type}: {why}")
    if (filters.customer_only and row.audience != "customer") or (
        filters.current_only and row.status != "current"
    ):
        return Attribution(
            "excluded_by_metadata_filter", f"audience={row.audience} status={row.status}"
        )
    return Attribution("ranked_below_k")
