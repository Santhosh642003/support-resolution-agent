"""The refund engine against the SEEDED DATABASE: rows -> `load_refund_order` -> `decide_refund`.

The engine is pure and is unit-tested on hand-built inputs; this is the step that connects it to what
the seed actually contains, so a mismatch between the data and the rules' expectations shows up here
(SPEC 18.7). One test per seeded refund case asserts the FULL Decision: outcome, per-line amounts,
aggregate, authorized amount, reason codes, and the decisive and supporting clause sets. `today` is
the pinned SIMULATED_TODAY.

Every test runs in a rolled-back transaction as the bootstrap superuser, so rows other tests commit
cannot enter: each assertion names the orders it is about (FAILURE_ANALYSIS F-006).
"""

from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Connection, text

from app.auth.context import CustomerContext
from app.clock import FixedClock
from app.domain.reason_codes import ReasonCode as R
from app.domain.rules.refund import (
    RefundDecision,
    RefundOrder,
    RefundOutcome,
    RefundReplacement,
    decide_refund,
)
from app.repositories.refund_loader import load_refund_order
from app.seed import ids
from app.seed.definitions import ORDERS, OrderDef
from app.seed.loader import seed_all
from tests.db.helpers import make_action, make_decision, make_refund
from tests.unit.refund_fixtures import from_seed

TODAY = FixedClock().today()  # 2026-04-15
ALLOWED, DENIED, HUMAN = RefundOutcome.ALLOWED, RefundOutcome.DENIED, RefundOutcome.NEEDS_HUMAN
BY_NUMBER = {o.order_number: o for o in ORDERS}


@pytest.fixture
def seeded(conn: Connection) -> Connection:
    seed_all(conn)
    return conn


def ctx_of(order: OrderDef) -> CustomerContext:
    return CustomerContext(ids.customer_id(order.customer))


def decide(conn: Connection, number: str) -> RefundDecision:
    order = load_refund_order(conn, ctx_of(BY_NUMBER[number]), number)
    assert order is not None, number
    return decide_refund(order, TODAY)


@dataclass(frozen=True)
class Expected:
    outcome: RefundOutcome
    version: int
    lines: dict[str, int]  # by sku: the amount each line would refund
    reasons: tuple[R, ...]
    decisive: frozenset[str]
    supporting: frozenset[str]
    authorized: int | None = None  # defaults to the aggregate when allowed, else 0

    @property
    def aggregate(self) -> int:
        return sum(self.lines.values())


def keys(*ks: str) -> frozenset[str]:
    return frozenset(ks)


EXPECTED: dict[str, Expected] = {
    # case 1: v1, day 28, above the 7500 v1 ceiling: human review, nothing authorized
    "SIM-1001": Expected(
        HUMAN,
        1,
        {"SIM-BOX-DELUXE": 5000, "SIM-BOX-FAMILY": 4800},
        (R.ABOVE_AUTO_APPROVE_CEILING, R.REFUND_WITHIN_WINDOW),
        keys("refund-v1-clause-3"),
        keys("refund-v1-clause-1"),
    ),  # fmt: skip
    # case 2: promo discount, paid not list; the 500 shipping line stays (the shipment arrived)
    "SIM-1002": Expected(
        ALLOWED,
        2,
        {"SIM-BOX-CLASSIC": 2250, "SIM-BOX-DELUXE": 3750, "SHIPPING": 0},
        (R.REFUND_WITHIN_WINDOW, R.SHIPPING_NOT_REFUNDABLE, R.PAID_AMOUNT_BASIS_POLICY),
        keys("refund-v2-clause-1", "refund-v2-clause-6", "refund-v2-clause-4"),
        keys("refund-v2-clause-2"),
    ),  # fmt: skip
    # case 3: the accessed digital add-on is excluded
    "SIM-1003": Expected(
        ALLOWED,
        2,
        {"SIM-ADDON-RECIPES": 0, "SIM-BOX-CLASSIC": 3000, "SIM-BOX-DELUXE": 5000},
        (R.REFUND_WITHIN_WINDOW,),
        keys("refund-v2-clause-1", "refund-v2-clause-5"),
        keys("refund-v2-clause-2"),
    ),  # fmt: skip
    # case 4: the final_sale mug is excluded, and so is the shipping line
    "SIM-1004": Expected(
        ALLOWED,
        2,
        {"SIM-BOX-CLASSIC": 3000, "SIM-CLEARANCE-MUG": 0, "SHIPPING": 0},
        (R.REFUND_WITHIN_WINDOW, R.SHIPPING_NOT_REFUNDABLE),
        keys("refund-v2-clause-1", "refund-v2-clause-5", "refund-v2-clause-6"),
        keys("refund-v2-clause-2"),
    ),  # fmt: skip
    # case 5: presumed lost, a replacement (SIM-2002) is in transit and is the remedy
    "SIM-2001": Expected(
        DENIED,
        2,
        {"SIM-BOX-FAMILY": 0, "SIM-BOX-MINI": 0},
        (R.REPLACEMENT_IS_REMEDY,),
        keys("shipping-v1-clause-4"),
        keys("shipping-v1-clause-2", "shipping-v1-clause-3", "refund-v2-clause-6"),
    ),  # fmt: skip
    # supplementary: v1 order delivered after the cutover, day 41 of 30
    "SIM-1005": Expected(
        DENIED,
        1,
        {"SIM-BOX-CLASSIC": 0, "SIM-BOX-MINI": 0, "SHIPPING": 0},
        (R.REFUND_WINDOW_EXPIRED,),
        keys("refund-v1-clause-1"),
        keys(),
    ),  # fmt: skip
    # supplementary: carrier exception one day before presumed lost; the whole shipment, so shipping too
    "SIM-3001": Expected(
        ALLOWED,
        2,
        {"SIM-BOX-DELUXE": 5000, "SHIPPING": 500},
        (
            R.REFUND_WITHIN_WINDOW,
            R.CARRIER_DELIVERY_EXCEPTION,
            R.EARLY_REFUND_ON_DELIVERY_EXCEPTION,
            R.SHIPPING_CHARGE_REFUNDED,
        ),
        keys("refund-v2-clause-3", "refund-v2-clause-1", "refund-v2-clause-6"),
        keys("shipping-v1-clause-2", "refund-v2-clause-2"),
    ),  # fmt: skip
    # supplementary: a box with 1 of 3 items shipped: 5000 * 2/3 = 3333.33, rounded UP to 3334
    "SIM-3003": Expected(
        ALLOWED,
        2,
        {"SIM-BOX-DELUXE": 3334, "SHIPPING": 0},
        (
            R.REFUND_WITHIN_WINDOW,
            R.SHIPPING_NOT_REFUNDABLE,
            R.PRORATED_BY_ITEMS_SHIPPED,
            R.RULE_CARRIED_FORWARD_FROM_V1,
        ),
        keys("refund-v2-clause-1", "refund-v2-clause-6"),
        keys("refund-v2-clause-2"),
    ),  # fmt: skip
    # supplementary: lost, replaced, the replacement is lost too: the original is refundable, shipping too
    "SIM-3004": Expected(
        ALLOWED,
        2,
        {"SIM-BOX-CLASSIC": 3000, "SHIPPING": 500},
        (
            R.REFUND_WITHIN_WINDOW,
            R.LOST_SHIPMENT_FULL_REFUND,
            R.RULE_CARRIED_FORWARD_FROM_V1,
            R.REPLACEMENT_ALSO_LOST,
            R.SHIPPING_CHARGE_REFUNDED,
        ),
        keys(
            "refund-v2-clause-1",
            "refund-v2-clause-6",
            "shipping-v1-clause-2",
            "shipping-v1-clause-4",
        ),
        keys("refund-v2-clause-2"),
    ),  # fmt: skip
    # not yet presumed lost (in transit): nothing to refund yet
    "SIM-2003": Expected(
        DENIED,
        2,
        {"SIM-BOX-CLASSIC": 0, "SHIPPING": 0},
        (R.NOT_YET_REFUNDABLE, R.NOT_YET_PRESUMED_LOST),
        keys("shipping-v1-clause-2", "refund-v2-clause-6"),
        keys(),
    ),  # fmt: skip
    # the two free replacements: nothing was paid, so no clause decides anything
    "SIM-2002": Expected(
        DENIED, 2, {"SIM-BOX-FAMILY": 0, "SIM-BOX-MINI": 0}, (R.NOTHING_WAS_PAID,), keys(), keys()
    ),  # fmt: skip
    "SIM-3005": Expected(
        DENIED, 2, {"SIM-BOX-CLASSIC": 0}, (R.NOTHING_WAS_PAID,), keys(), keys()
    ),  # fmt: skip
    # a gift card: no shipment, so no window anchor: a human
    "SIM-3002": Expected(
        HUMAN, 2, {"SIM-GIFTCARD-50": 0}, (R.NO_WINDOW_ANCHOR,), keys(), keys()
    ),  # fmt: skip
}
SUBSCRIPTION_CHARGES = [o.order_number for o in ORDERS if o.kind == "subscription_charge"]


def test_every_seeded_order_is_covered_here() -> None:
    """A new seeded order must get an expectation (or be a subscription charge) before this passes."""
    assert set(EXPECTED) | set(SUBSCRIPTION_CHARGES) == set(BY_NUMBER)


@pytest.mark.parametrize("number", sorted(EXPECTED))
def test_the_full_decision_for_each_seeded_order_loaded_from_the_database(
    seeded: Connection, number: str
) -> None:
    want = EXPECTED[number]
    d = decide(seeded, number)
    assert d.outcome is want.outcome, number
    assert d.policy_version == want.version
    assert {line.sku: line.amount_cents for line in d.lines} == want.lines
    assert d.aggregate_cents == want.aggregate
    assert d.authorized_cents == (want.aggregate if want.outcome is ALLOWED else 0)
    assert d.reason_codes == want.reasons
    assert frozenset(d.decisive_clause_keys) == want.decisive
    assert frozenset(d.supporting_clause_keys) == want.supporting


@pytest.mark.parametrize("number", SUBSCRIPTION_CHARGES)
def test_subscription_charges_are_routed_to_the_cancellation_rules(
    seeded: Connection, number: str
) -> None:
    d = decide(seeded, number)
    assert d.outcome is HUMAN and d.reason_codes == (R.SUBSCRIPTION_CHARGE_USES_CANCELLATION_RULES,)


# --- the loader reads what the rules need ------------------------------------------------------------


def test_the_loader_reads_shipping_lines_box_counts_and_linked_replacements(
    seeded: Connection,
) -> None:
    o = load_refund_order(seeded, ctx_of(BY_NUMBER["SIM-3003"]), "SIM-3003")
    assert o is not None
    box, ship = o.items
    assert (box.line_type, box.items_total, box.items_shipped, box.paid_cents) == (
        "item",
        3,
        1,
        5000,
    )
    assert (ship.line_type, ship.sku, ship.paid_cents) == ("shipping", "SHIPPING", 500)
    assert ship.items_total is None and not ship.final_sale

    lost = load_refund_order(seeded, ctx_of(BY_NUMBER["SIM-3004"]), "SIM-3004")
    assert lost is not None and len(lost.replacements) == 1
    rep = lost.replacements[0]
    assert (rep.order_number, rep.status) == ("SIM-3005", "shipped")
    assert rep.shipment is not None and rep.shipment.shipped_on.isoformat() == "2026-03-23"  # type: ignore[union-attr]
    assert rep.shipment.delivered_on is None

    exc = load_refund_order(seeded, ctx_of(BY_NUMBER["SIM-3001"]), "SIM-3001")
    assert exc is not None and exc.shipment is not None and exc.shipment.has_delivery_exception
    addon = load_refund_order(seeded, ctx_of(BY_NUMBER["SIM-1003"]), "SIM-1003")
    assert addon is not None
    accessed = [i for i in addon.items if i.digital_accessed_on is not None]
    assert [(i.sku, i.digital_accessed_on.isoformat()) for i in accessed] == [  # type: ignore[union-attr]
        ("SIM-ADDON-RECIPES", "2026-03-30")
    ]
    assert all(i.refunded_cents == 0 for i in addon.items) and addon.refunded_total_cents == 0


def test_an_order_that_is_not_the_customers_is_not_found(seeded: Connection) -> None:
    blair = CustomerContext(ids.customer_id("blair"))
    assert load_refund_order(seeded, blair, "SIM-1001") is None  # Alex's order
    assert load_refund_order(seeded, blair, "SIM-9999") is None  # no such order
    assert load_refund_order(seeded, blair, "SIM-2001") is not None  # the control: hers


@pytest.mark.parametrize("number", sorted(EXPECTED) + SUBSCRIPTION_CHARGES)
def test_the_database_loader_agrees_with_the_definition_based_fixture(
    seeded: Connection, number: str
) -> None:
    """Two independent routes to a RefundOrder (the seed DEFINITIONS the unit tests use, and the
    ROWS the loader reads) must describe the same order. Lines are compared by (type, sku) because
    the database has no definition order."""
    loaded = load_refund_order(seeded, ctx_of(BY_NUMBER[number]), number)
    built = from_seed(number)
    assert loaded is not None

    def norm(o: RefundOrder) -> tuple[RefundOrder, list[Any]]:
        items = sorted(
            (i.line_type, i.sku, i.category, i.paid_cents, i.refunded_cents, i.final_sale,
             i.digital_addon, i.digital_accessed_on, i.items_total, i.items_shipped)
            for i in o.items
        )  # fmt: skip
        reps = tuple(
            RefundReplacement(r.order_number, r.status, r.shipment) for r in o.replacements
        )
        return replace(o, items=(), replacements=reps), items

    assert norm(loaded) == norm(built)


# --- prior refunds in the database change the next decision (the cumulative ceiling, end to end) ----


def _refund_line(conn: Connection, number: str, sku: str, amount: int) -> None:
    """Record a real refund of `amount` on one line of a seeded order, through the real tables."""
    order = BY_NUMBER[number]
    customer, order_id = ids.customer_id(order.customer), ids.order_id(order.key)
    line_id: UUID = conn.execute(
        text(
            "SELECT i.id FROM order_items i JOIN products p ON p.id = i.product_id"
            " WHERE i.order_id = :o AND p.sku = :s"
        ),
        {"o": order_id, "s": sku},
    ).scalar_one()
    action = make_action(
        conn, customer, make_decision(conn, customer, order_id, amount=amount), amount=amount
    )
    make_refund(conn, customer, order_id, action, amount, lines=[(line_id, amount)])


def test_refund_splitting_end_to_end_the_second_request_goes_to_a_human(seeded: Connection) -> None:
    """SIM-1001 (v1, ceiling 7500): 5000 was already refunded on the deluxe box. The family box's 4800
    is below the ceiling on its own, but 9800 cumulative is not: human review, citing the ceiling."""
    _refund_line(seeded, "SIM-1001", "SIM-BOX-DELUXE", 5000)
    d = decide(seeded, "SIM-1001")
    assert {line.sku: line.amount_cents for line in d.lines} == {
        "SIM-BOX-DELUXE": 0,
        "SIM-BOX-FAMILY": 4800,
    }
    assert d.outcome is HUMAN and d.authorized_cents == 0
    assert d.reason_codes[:2] == (R.ABOVE_AUTO_APPROVE_CEILING, R.CUMULATIVE_REFUNDS_ABOVE_CEILING)
    assert d.handoff is not None
    assert d.handoff.evidence["prior_refunded_cents"] == 5000
    assert d.handoff.evidence["cumulative_cents"] == 9800
    assert d.decisive_clause_keys == ("refund-v1-clause-3",)


def test_a_refunded_line_is_not_refunded_again_and_a_small_second_request_is_allowed(
    seeded: Connection,
) -> None:
    """SIM-1003 (v2, ceiling 12000): refund the deluxe box first; the rest (3000) is within the
    ceiling cumulatively (5000 + 3000), so it is allowed and the refunded line contributes nothing."""
    _refund_line(seeded, "SIM-1003", "SIM-BOX-DELUXE", 5000)
    d = decide(seeded, "SIM-1003")
    assert {line.sku: line.amount_cents for line in d.lines} == {
        "SIM-ADDON-RECIPES": 0,
        "SIM-BOX-CLASSIC": 3000,
        "SIM-BOX-DELUXE": 0,
    }
    assert d.outcome is ALLOWED and d.aggregate_cents == 3000
    assert d.facts["prior_refunded_cents"] == 5000 and d.facts["cumulative_cents"] == 8000


def test_a_lost_order_refunded_in_full_cannot_be_refunded_again(seeded: Connection) -> None:
    """SIM-3004, shipping line included (3500 of 3500 paid), then asked again: nothing is left."""
    _refund_line(seeded, "SIM-3004", "SIM-BOX-CLASSIC", 3000)
    number = "SIM-3004"
    order = BY_NUMBER[number]
    customer, order_id = ids.customer_id(order.customer), ids.order_id(order.key)
    ship_line: UUID = seeded.execute(
        text("SELECT id FROM order_items WHERE order_id = :o AND line_type = 'shipping'"),
        {"o": order_id},
    ).scalar_one()
    action = make_action(
        seeded, customer, make_decision(seeded, customer, order_id, amount=500), amount=500
    )
    make_refund(seeded, customer, order_id, action, 500, lines=[(ship_line, 500)])
    d = decide(seeded, number)
    assert d.outcome is DENIED and d.aggregate_cents == 0
    assert {line.sku: line.amount_cents for line in d.lines} == {
        "SIM-BOX-CLASSIC": 0,
        "SHIPPING": 0,
    }
    assert all(line.reason_codes[-1] is R.ALREADY_REFUNDED for line in d.lines)
