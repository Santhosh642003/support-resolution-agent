"""The 2026-11-12 policy amendments and what they changed in the refund rules (SPEC 18.6):

  * v2 has a RESIDUAL clause (6) with incorporation links, so carried-forward rules are citable;
  * a replacement already sent is the remedy (shipping v1 clause 4): denied, a clean determination;
  * the auto-approve ceiling is CUMULATIVE per order (refund splitting is an adversarial case);
  * proration rounds UP in the customer's favour, capped at the line's paid amount, and fires end to
    end on a seeded partially shipped box;
  * shipping is an order line, refundable only when the entire shipment is lost or undelivered.

No database, no network.
"""

from datetime import date, timedelta

import pytest

from app.clock import FixedClock
from app.domain.reason_codes import ReasonCode as R
from app.domain.rules.refund import (
    RefundDecision,
    RefundItem,
    RefundOrder,
    RefundOutcome,
    RefundReplacement,
    RefundShipment,
    decide_refund,
)
from app.domain.rules.shipping import presumed_lost_date
from app.domain.types import PurchaseDate
from tests.unit.refund_fixtures import delivered_order, from_seed

TODAY = FixedClock().today()  # 2026-04-15
ALLOWED, DENIED, HUMAN = RefundOutcome.ALLOWED, RefundOutcome.DENIED, RefundOutcome.NEEDS_HUMAN
V1_DAY = date(2026, 2, 1)
V2_DAY = date(2026, 4, 1)
SHIPPED = date(2026, 3, 11)  # presumed lost 2026-03-25 (10 business days), day 21 of 45 on TODAY


def lines(d: RefundDecision) -> list[int]:
    return [ln.amount_cents for ln in d.lines]


# --- the replacement is the remedy (shipping v1 clause 4) -------------------------------------------


def lost_order(
    *replacements: RefundReplacement, shipping: int = 500, status: str = "in_transit"
) -> RefundOrder:
    items: tuple[RefundItem, ...] = (
        RefundItem("a", "A", "general", 4800),
        RefundItem("b", "B", "general", 2200),
    )
    if shipping:
        items += (RefundItem("s", "SHIPPING", "shipping", shipping, line_type="shipping"),)
    return RefundOrder(
        "T-LOST",
        PurchaseDate(date(2026, 3, 10)),
        "shipped",
        items,
        RefundShipment(status, SHIPPED, None),
        paid_cents=7000 + shipping,
        replacements=replacements,
    )


def replacement(
    shipped: date | None, *, status: str = "shipped", ship_status: str = "in_transit",
    delivered: date | None = None,
) -> RefundReplacement:  # fmt: skip
    shipment = None if shipped is None else RefundShipment(ship_status, shipped, delivered)
    return RefundReplacement("T-REPL", status, shipment)


def test_a_lost_shipment_with_no_replacement_is_refunded_in_full_including_shipping() -> None:
    d = decide_refund(lost_order(), TODAY)
    assert d.outcome is ALLOWED and lines(d) == [4800, 2200, 500] and d.aggregate_cents == 7500
    assert R.SHIPPING_CHARGE_REFUNDED in d.reason_codes
    assert R.REPLACEMENT_IS_REMEDY not in d.reason_codes


def test_a_replacement_in_transit_is_the_remedy_and_the_denial_names_it_and_its_window() -> None:
    d = decide_refund(lost_order(replacement(date(2026, 4, 6))), TODAY)
    assert d.outcome is DENIED and d.handoff is None  # a clean determination, not needs_human
    assert d.reason_codes == (R.REPLACEMENT_IS_REMEDY,) and d.authorized_cents == 0
    assert d.facts["replacement_order"] == "T-REPL"
    assert (
        d.facts["replacement_presumed_lost_on"] == presumed_lost_date(date(2026, 4, 6)).isoformat()
    )
    assert d.decisive_clause_keys == ("shipping-v1-clause-4",)


def test_a_replacement_that_arrived_is_also_the_remedy() -> None:
    delivered = replacement(date(2026, 4, 1), status="delivered", ship_status="delivered",
                            delivered=date(2026, 4, 8))  # fmt: skip
    d = decide_refund(lost_order(delivered), TODAY)
    assert d.outcome is DENIED and d.reason_codes == (R.REPLACEMENT_IS_REMEDY,)


def test_the_chain_lost_replaced_replacement_lost_then_the_refund_becomes_available() -> None:
    """The boundary is the replacement's OWN presumed-lost date: one day before, still the remedy;
    on that date, the original is refundable (and the denial reason disappears)."""
    lost_on = presumed_lost_date(date(2026, 4, 1))  # the replacement shipped 2026-04-01
    assert lost_on == TODAY  # exactly today: the boundary
    on_the_day = decide_refund(lost_order(replacement(date(2026, 4, 1))), TODAY)
    assert on_the_day.outcome is ALLOWED and lines(on_the_day) == [4800, 2200, 500]
    assert R.REPLACEMENT_ALSO_LOST in on_the_day.reason_codes
    assert R.REPLACEMENT_IS_REMEDY not in on_the_day.reason_codes
    assert "shipping-v1-clause-4" in on_the_day.decisive_clause_keys
    day_before = decide_refund(lost_order(replacement(date(2026, 4, 1))), TODAY - timedelta(days=1))
    assert day_before.outcome is DENIED and day_before.reason_codes == (R.REPLACEMENT_IS_REMEDY,)
    # the original's own window still governs the refund once it is available: day 21 of 45 here
    assert on_the_day.facts["window_anchor"] == "2026-03-25"


def test_a_carrier_status_of_lost_on_the_replacement_ends_its_status_as_the_remedy() -> None:
    d = decide_refund(lost_order(replacement(date(2026, 4, 10), ship_status="lost")), TODAY)
    assert d.outcome is ALLOWED and R.REPLACEMENT_ALSO_LOST in d.reason_codes


def test_a_canceled_replacement_was_never_a_remedy() -> None:
    d = decide_refund(lost_order(replacement(None, status="canceled")), TODAY)
    assert d.outcome is ALLOWED


def test_a_replacement_not_yet_shipped_is_still_pending_so_it_is_the_remedy() -> None:
    d = decide_refund(lost_order(replacement(None, status="placed")), TODAY)
    assert d.outcome is DENIED and d.reason_codes == (R.REPLACEMENT_IS_REMEDY,)
    assert "replacement_presumed_lost_on" not in d.facts


def test_with_two_replacements_one_lost_one_in_transit_the_remedy_still_stands() -> None:
    lost = replacement(date(2026, 3, 20))
    live = replacement(date(2026, 4, 9))
    assert decide_refund(lost_order(lost, live), TODAY).outcome is DENIED
    assert decide_refund(lost_order(lost, lost), TODAY).outcome is ALLOWED


def test_the_rule_does_not_apply_to_a_delivered_original_it_is_recorded_only() -> None:
    o = delivered_order(V2_DAY, TODAY - timedelta(days=3), 3000)
    o = RefundOrder(**{**o.__dict__, "replacements": (replacement(date(2026, 4, 10)),)})
    d = decide_refund(o, TODAY)
    assert d.outcome is ALLOWED and R.REPLACEMENT_ALREADY_SENT in d.reason_codes


def test_a_delivery_exception_early_refund_is_also_blocked_by_a_replacement() -> None:
    o = RefundOrder(
        "T-EXC", PurchaseDate(V2_DAY), "shipped", (RefundItem("a", "A", "general", 3000),),
        RefundShipment("exception", date(2026, 4, 10), None, has_delivery_exception=True),
        paid_cents=3000, replacements=(replacement(date(2026, 4, 13)),),
    )  # fmt: skip
    assert decide_refund(o, TODAY).reason_codes == (R.REPLACEMENT_IS_REMEDY,)
    assert (
        decide_refund(RefundOrder(**{**o.__dict__, "replacements": ()}), TODAY).outcome is ALLOWED
    )


def test_the_replacement_is_not_double_counted_against_the_paid_amount() -> None:
    """The replacement carries paid 0 (SIM-2002), so refunding the original once it is available can
    never exceed the original's paid amount."""
    d = decide_refund(lost_order(replacement(date(2026, 4, 1))), TODAY)
    assert d.aggregate_cents <= 7500


# --- the ceiling is cumulative per order -----------------------------------------------------------


def after_prior_refund(prior: int, remaining: int, version_day: date = V2_DAY) -> RefundOrder:
    """Line a was fully refunded earlier (`prior` cents); line b is what is left to ask for."""
    return RefundOrder(
        "T-PRIOR", PurchaseDate(version_day), "delivered",
        (RefundItem("a", "A", "general", prior, prior), RefundItem("b", "B", "general", remaining)),
        RefundShipment("delivered", TODAY - timedelta(days=6), TODAY - timedelta(days=3)),
        paid_cents=prior + remaining, refunded_total_cents=prior,
    )  # fmt: skip


@pytest.mark.parametrize(("version_day", "ceiling"), [(V1_DAY, 7500), (V2_DAY, 12000)])
def test_adversarial_family_7_refund_splitting_two_requests_each_below_the_ceiling(
    version_day: date, ceiling: int
) -> None:
    """Two sequential requests, each below the ceiling alone, summing above it. The second goes to
    human review although the request on its own is under the limit (SPEC 18.6, decision 3)."""
    each = ceiling - 500
    ship = RefundShipment("delivered", TODAY - timedelta(days=6), TODAY - timedelta(days=3))
    when = PurchaseDate(version_day)

    # Request 1: one line is refundable, 'each' cents, below the ceiling: auto-approved.
    first = RefundOrder(
        "T-SPLIT", when, "delivered", (RefundItem("a", "A", "general", each),), ship,
        paid_cents=each,
    )  # fmt: skip
    d1 = decide_refund(first, TODAY)
    assert d1.outcome is ALLOWED and d1.aggregate_cents == each < ceiling

    # Request 2: the same order's other line, with request 1's refund now recorded on the order.
    second = RefundOrder(
        "T-SPLIT", when, "delivered",
        (RefundItem("a", "A", "general", each, each), RefundItem("b", "B", "general", each)),
        ship, paid_cents=2 * each, refunded_total_cents=each,
    )  # fmt: skip
    d2 = decide_refund(second, TODAY)
    assert d2.aggregate_cents == each < ceiling  # alone, request 2 is below the ceiling ...
    assert each + each > ceiling  # ... the pair is not
    assert d2.outcome is HUMAN and d2.authorized_cents == 0
    assert d2.reason_codes[:2] == (R.ABOVE_AUTO_APPROVE_CEILING, R.CUMULATIVE_REFUNDS_ABOVE_CEILING)
    assert d2.handoff is not None
    assert d2.handoff.evidence["prior_refunded_cents"] == each
    assert d2.handoff.evidence["cumulative_cents"] == 2 * each
    assert d2.handoff.evidence["proposed_cents"] == each
    assert d2.facts["prior_refunded_cents"] == each


@pytest.mark.parametrize(("version_day", "ceiling"), [(V1_DAY, 7500), (V2_DAY, 12000)])
def test_the_cumulative_ceiling_from_both_sides(version_day: date, ceiling: int) -> None:
    prior = 3000
    room = ceiling - prior  # what the ceiling still allows on this order
    below = decide_refund(after_prior_refund(prior, room - 1, version_day), TODAY)
    at = decide_refund(after_prior_refund(prior, room, version_day), TODAY)
    over = decide_refund(after_prior_refund(prior, room + 1, version_day), TODAY)
    assert below.outcome is ALLOWED and at.outcome is ALLOWED  # exactly at the ceiling: allowed
    assert over.outcome is HUMAN and over.reason_codes[0] is R.ABOVE_AUTO_APPROVE_CEILING
    assert over.reason_codes[1] is R.CUMULATIVE_REFUNDS_ABOVE_CEILING
    ceiling_clause = {1: "refund-v1-clause-3", 2: "refund-v2-clause-2"}[over.policy_version]
    assert over.decisive_clause_keys == (ceiling_clause,)


def test_a_request_alone_above_the_ceiling_is_not_labelled_cumulative() -> None:
    d = decide_refund(
        delivered_order(V1_DAY, TODAY - timedelta(days=3), 9000), TODAY
    )  # no prior refunds
    assert d.outcome is HUMAN and d.reason_codes[0] is R.ABOVE_AUTO_APPROVE_CEILING
    assert R.CUMULATIVE_REFUNDS_ABOVE_CEILING not in d.reason_codes


def test_prior_refunds_do_not_block_a_request_that_stays_within_the_ceiling() -> None:
    assert decide_refund(after_prior_refund(2000, 1000), TODAY).outcome is ALLOWED  # 3000 <= 12000
    assert decide_refund(after_prior_refund(11000, 2000), TODAY).outcome is HUMAN  # 13000 > 12000


# --- proration: round up, cap at paid, and the seeded case ------------------------------------------


@pytest.mark.parametrize(
    ("paid", "total", "shipped", "expected"),
    [
        (5000, 3, 1, 3334),  # 3333.33 -> up
        (5000, 3, 2, 1667),  # 1666.67 -> up
        (1001, 3, 1, 668),  # 667.33 -> up
        (3000, 4, 1, 2250),  # exact: no rounding
        (3000, 4, 0, 3000),  # nothing shipped: the whole line, capped at paid
        (3000, 4, 4, 0),  # everything shipped: nothing
        (1, 3, 2, 1),  # 0.33 -> 1: up, never 0 while something is unshipped
        (100, 7, 3, 58),  # 57.14 -> up
    ],
)
def test_proration_rounds_up_in_the_customers_favour_and_never_exceeds_paid(
    paid: int, total: int, shipped: int, expected: int
) -> None:
    o = delivered_order(
        V2_DAY, TODAY - timedelta(days=3), paid, items_total=total, items_shipped=shipped
    )
    d = decide_refund(o, TODAY)
    assert lines(d) == [expected]
    assert expected <= paid
    assert expected >= paid * (total - shipped) // total  # never below the rounded-down figure


def test_the_seeded_partially_shipped_box_makes_proration_fire_end_to_end() -> None:
    o = from_seed("partially_shipped_subscription_box")  # SIM-3003: 3 items, 1 shipped, paid 5000
    box = o.items[0]
    assert (box.items_total, box.items_shipped, box.paid_cents) == (3, 1, 5000)
    d = decide_refund(o, TODAY)
    assert d.outcome is ALLOWED and d.policy_version == 2
    assert lines(d) == [3334, 0]  # 5000 * 2/3 rounded UP (down would be 3333); shipping stays
    assert d.lines[0].reason_codes == (R.PRORATED_BY_ITEMS_SHIPPED,)
    assert d.lines[1].reason_codes == (R.SHIPPING_NOT_REFUNDABLE,)
    assert R.RULE_CARRIED_FORWARD_FROM_V1 in d.reason_codes
    by_key = {c.key: c for c in d.applied}
    assert by_key["refund-v2-clause-6"].incorporating == (
        "refund-v1-clause-2",
        "refund-v1-clause-4",
    )
    assert d.decisive_clause_keys == ("refund-v2-clause-1", "refund-v2-clause-6")


# --- shipping is an order line ----------------------------------------------------------------------


def test_shipping_is_refunded_only_when_the_entire_shipment_is_lost_or_undelivered() -> None:
    delivered = decide_refund(from_seed("promo_discount_paid_differs_from_list"), TODAY)
    assert delivered.lines[-1].reason_codes == (R.SHIPPING_NOT_REFUNDABLE,)
    assert delivered.lines[-1].amount_cents == 0
    exception = decide_refund(from_seed("v2_delivery_exception_before_presumed_lost"), TODAY)
    assert lines(exception) == [5000, 500] and exception.authorized_cents == 5500  # all of it
    assert exception.authorized_cents == from_seed("SIM-3001").paid_cents


def test_a_shipping_line_is_capped_by_the_paid_amount_invariant_like_any_line() -> None:
    o = lost_order(shipping=500)
    already = RefundOrder(
        **{**o.__dict__,
           "items": (*o.items[:2], RefundItem("s", "SHIPPING", "shipping", 500, 300, line_type="shipping")),
           "refunded_total_cents": 300}
    )  # fmt: skip
    d = decide_refund(already, TODAY)
    assert lines(d) == [4800, 2200, 200]  # only the 200 still unrefunded
    assert d.aggregate_cents + already.refunded_total_cents <= already.paid_cents


def test_when_only_the_shipping_line_is_left_on_a_delivered_order_nothing_is_refundable() -> None:
    o = delivered_order(V2_DAY, TODAY - timedelta(days=3), 3000, final_sale=True, shipping=500)
    o = RefundOrder(
        **{**o.__dict__,
           "items": (*o.items, RefundItem("s", "SHIPPING", "shipping", 500, line_type="shipping"))}
    )  # fmt: skip
    d = decide_refund(o, TODAY)
    assert d.outcome is DENIED and R.NOTHING_REFUNDABLE in d.reason_codes
    assert set(d.decisive_clause_keys) == {"refund-v2-clause-5", "refund-v2-clause-6"}


def test_a_shipping_line_cannot_be_final_sale_digital_or_a_box() -> None:
    for kw in (
        {"final_sale": True},
        {"digital_addon": True},
        {"items_total": 2, "items_shipped": 1},
    ):
        with pytest.raises(ValueError, match="shipping line"):
            RefundItem("s", "S", "shipping", 500, line_type="shipping", **kw)


# --- residual clause: carried-forward rules are citable ---------------------------------------------


def test_a_v2_lost_shipment_cites_clause_6_incorporating_v1_clause_2() -> None:
    d = decide_refund(lost_order(), TODAY)
    assert d.policy_version == 2
    by_key = {c.key: c for c in d.applied}
    assert by_key["refund-v2-clause-6"].decisive
    assert by_key["refund-v2-clause-6"].incorporating == ("refund-v1-clause-2",)
    assert R.RULE_CARRIED_FORWARD_FROM_V1 in d.reason_codes
    assert "refund-v2-clause-1" in d.decisive_clause_keys  # the window and its anchor are v2's own


def test_a_v1_lost_shipment_cites_its_own_clause_2_with_no_incorporation() -> None:
    o = RefundOrder(**{**lost_order().__dict__, "purchase_date": PurchaseDate(V1_DAY)})
    d = decide_refund(o, TODAY)
    assert d.policy_version == 1 and R.RULE_CARRIED_FORWARD_FROM_V1 not in d.reason_codes
    assert "refund-v1-clause-2" in d.decisive_clause_keys
    assert all(c.incorporating == () for c in d.applied)
