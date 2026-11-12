"""The order-status rules: pure, deterministic, and they record which clauses they applied."""

from dataclasses import replace
from datetime import date

import pytest

from app.domain.reason_codes import ReasonCode, unverifiable_claim_evidence
from app.domain.rules.order_status import (
    RULE_SET_VERSION,
    OrderStatusInput,
    OrderStatusOutcome,
    ShipmentInput,
    determine_order_status,
)
from app.domain.rules.shipping import SHIPPING_POLICY_VERSION
from app.retrieval.policy_loader import load_policies

SHIPPED = date(2026, 3, 11)  # Wednesday: expected 03-20, presumed lost 03-25
EXPECTED = date(2026, 3, 20)
LOST_ON = date(2026, 3, 25)


def order(**over: object) -> OrderStatusInput:
    base = OrderStatusInput(
        order_number="SIM-T",
        order_status="shipped",
        purchase_date=date(2026, 3, 10),
        is_replacement=False,
        categories=frozenset({"general"}),
        shipment=ShipmentInput("in_transit", SHIPPED, None, EXPECTED, False),
    )
    return replace(base, **over)  # type: ignore[arg-type]


def ship(**over: object) -> ShipmentInput:
    return replace(ShipmentInput("in_transit", SHIPPED, None, EXPECTED, False), **over)  # type: ignore[arg-type]


def det(inp: OrderStatusInput, today: date, version: int = SHIPPING_POLICY_VERSION):  # type: ignore[no-untyped-def]
    return determine_order_status(inp, today, version)


# --- the three questions the task asks: inside the window, late, or presumed lost ------------------


@pytest.mark.parametrize(
    ("today", "outcome", "reasons"),
    [
        (
            date(2026, 3, 13),
            OrderStatusOutcome.IN_TRANSIT_ON_SCHEDULE,
            [ReasonCode.WITHIN_DELIVERY_WINDOW],
        ),
        (
            EXPECTED,
            OrderStatusOutcome.IN_TRANSIT_ON_SCHEDULE,
            [ReasonCode.WITHIN_DELIVERY_WINDOW],
        ),  # due today
        (
            date(2026, 3, 21),
            OrderStatusOutcome.LATE,
            [ReasonCode.PAST_DELIVERY_WINDOW, ReasonCode.NOT_YET_PRESUMED_LOST],
        ),
        (
            date(2026, 3, 24),
            OrderStatusOutcome.LATE,
            [ReasonCode.PAST_DELIVERY_WINDOW, ReasonCode.NOT_YET_PRESUMED_LOST],
        ),
        (
            LOST_ON,
            OrderStatusOutcome.PRESUMED_LOST,
            [ReasonCode.PRESUMED_LOST, ReasonCode.UNDELIVERED_PAST_PRESUMED_LOST_DATE],
        ),
        (
            date(2026, 4, 15),
            OrderStatusOutcome.PRESUMED_LOST,
            [ReasonCode.PRESUMED_LOST, ReasonCode.UNDELIVERED_PAST_PRESUMED_LOST_DATE],
        ),
    ],
)
def test_undelivered_shipment_window_late_or_presumed_lost(
    today: date, outcome: OrderStatusOutcome, reasons: list[ReasonCode]
) -> None:
    r = det(order(), today)
    assert r.outcome is outcome and list(r.reason_codes) == reasons
    assert r.applied_clauses == (1, 2)  # the window and the presumed-lost rule were both applied
    assert r.applied_clause_keys == ("shipping-v1-clause-1", "shipping-v1-clause-2")
    assert r.decision_outcome == "informational"  # a determination authorises no action


def test_the_boundaries_are_exact_day_by_day() -> None:
    by_day = {d: det(order(), date(2026, 3, d)).outcome for d in range(18, 28)}
    assert by_day[20] is OrderStatusOutcome.IN_TRANSIT_ON_SCHEDULE  # on the expected date
    assert by_day[21] is OrderStatusOutcome.LATE  # the first day past it
    assert by_day[24] is OrderStatusOutcome.LATE  # the day before presumed lost
    assert by_day[25] is OrderStatusOutcome.PRESUMED_LOST  # 10 business days after shipping


def test_the_dates_are_derived_from_the_ship_date_and_the_injected_today() -> None:
    f = det(order(), date(2026, 3, 22)).facts
    assert (f["shipped_on"], f["expected_on"], f["presumed_lost_on"]) == (
        "2026-03-11",
        "2026-03-20",
        "2026-03-25",
    )
    assert (
        f["as_of"] == "2026-03-22"
        and f["days_past_expected"] == 2
        and f["days_until_presumed_lost"] == 3
    )


def test_the_same_inputs_on_another_day_give_the_other_answer() -> None:
    assert det(order(), date(2026, 3, 22)).outcome is OrderStatusOutcome.LATE
    assert det(order(), date(2026, 3, 26)).outcome is OrderStatusOutcome.PRESUMED_LOST


# --- delivered, exception, replacement, and nothing-to-measure cases ---------------------------------


def test_delivered_in_and_after_the_window() -> None:
    on_time = det(
        order(shipment=ship(status="delivered", delivered_on=date(2026, 3, 18))), date(2026, 4, 15)
    )
    assert on_time.outcome is OrderStatusOutcome.DELIVERED
    assert ReasonCode.DELIVERED_WITHIN_WINDOW in on_time.reason_codes
    assert on_time.applied_clauses == (1,)  # delivered: the presumed-lost rule is not applied
    late = det(
        order(shipment=ship(status="delivered", delivered_on=date(2026, 3, 24))), date(2026, 4, 15)
    )
    assert (
        ReasonCode.DELIVERED_AFTER_WINDOW in late.reason_codes and late.facts["days_late"] == 4
    )  # calendar days past 03-20


def test_a_carrier_exception_before_the_presumed_lost_date() -> None:
    r = det(
        order(shipment=ship(status="exception", has_delivery_exception=True)), date(2026, 3, 24)
    )
    assert r.outcome is OrderStatusOutcome.DELIVERY_EXCEPTION
    assert list(r.reason_codes) == [
        ReasonCode.CARRIER_DELIVERY_EXCEPTION,
        ReasonCode.NOT_YET_PRESUMED_LOST,
    ]
    # ...but once the presumed-lost date arrives, that governs
    assert (
        det(order(shipment=ship(status="exception", has_delivery_exception=True)), LOST_ON).outcome
        is OrderStatusOutcome.PRESUMED_LOST
    )


def test_a_carrier_reported_loss_is_presumed_lost_with_its_own_reason() -> None:
    r = det(order(shipment=ship(status="lost")), date(2026, 3, 13))
    assert r.outcome is OrderStatusOutcome.PRESUMED_LOST and list(r.reason_codes) == [
        ReasonCode.CARRIER_REPORTED_LOST
    ]


def test_a_replacement_carries_its_own_window_and_clause_three_is_applied() -> None:
    r = det(order(is_replacement=True), date(2026, 3, 13))
    assert r.applied_clauses == (1, 2, 3) and ReasonCode.REPLACEMENT_OWN_WINDOW in r.reason_codes
    assert r.applied_clause_keys[-1] == "shipping-v1-clause-3"


def test_nothing_to_measure_applies_no_clause() -> None:
    assert (
        det(order(order_status="placed", shipment=None), date(2026, 4, 1)).outcome
        is OrderStatusOutcome.NOT_SHIPPED
    )
    digital = det(order(order_status="delivered", shipment=None), date(2026, 4, 1))
    assert digital.outcome is OrderStatusOutcome.DELIVERED_DIGITAL and digital.applied_clauses == ()
    canceled = det(order(order_status="canceled"), date(2026, 4, 1))
    assert canceled.outcome is OrderStatusOutcome.CANCELED and canceled.applied_clause_keys == ()
    label_only = det(
        order(shipment=ship(status="label_created", shipped_on=None)), date(2026, 4, 1)
    )
    assert label_only.outcome is OrderStatusOutcome.NOT_SHIPPED


# --- safe by default: refuse to guess -----------------------------------------------------------------


def test_a_policy_version_the_code_does_not_implement_needs_a_human() -> None:
    r = det(order(), date(2026, 4, 1), version=2)
    assert r.outcome is OrderStatusOutcome.NEEDS_HUMAN and r.decision_outcome == "needs_human"
    assert (
        list(r.reason_codes) == [ReasonCode.POLICY_VERSION_NOT_IMPLEMENTED]
        and r.applied_clauses == ()
    )


@pytest.mark.parametrize("category", ["hazardous", "perishable", "oversize", "age_restricted"])
def test_restricted_categories_are_not_judged_by_the_general_window(category: str) -> None:
    """OPEN ITEM carried to M2: category timelines differ from the general window and the rule
    refuses to apply the wrong one."""
    r = det(order(categories=frozenset({category})), date(2026, 4, 1))
    assert r.outcome is OrderStatusOutcome.NEEDS_HUMAN
    assert list(r.reason_codes) == [ReasonCode.CATEGORY_TIMELINE_NOT_IMPLEMENTED]
    delivered = det(
        order(
            categories=frozenset({category}),
            shipment=ship(status="delivered", delivered_on=date(2026, 3, 18)),
        ),
        date(2026, 4, 1),
    )
    assert (
        delivered.outcome is OrderStatusOutcome.DELIVERED
    )  # a delivery is a fact, no timeline needed


# --- consistency with the stored policy, and the standing rule -----------------------------------------


def test_the_rule_cites_clauses_that_exist_with_the_text_it_implements() -> None:
    docs = {(d.policy_type, d.version): d for d in load_policies()}
    shipping = docs[("shipping", SHIPPING_POLICY_VERSION)]
    r = det(order(is_replacement=True), date(2026, 3, 13))
    texts = {shipping.chunk_key(c.number): c.text for c in shipping.clauses}
    assert set(r.applied_clause_keys) <= set(texts)
    assert "7 business days" in texts["shipping-v1-clause-1"]
    assert "presumed lost" in texts["shipping-v1-clause-2"]
    assert "own window" in texts["shipping-v1-clause-3"]


def test_the_rule_set_version_is_recorded() -> None:
    assert RULE_SET_VERSION == "order-status-1"


def test_the_standing_rule_for_unverifiable_claims_has_a_reason_code_and_keeps_text_verbatim() -> (
    None
):
    claim = "I paid for this with a gift card, my cousin told me it counts as store credit"
    ev = unverifiable_claim_evidence([claim, "it was delivered to the wrong flat"])
    assert (
        ev["reason_code"] == "UNVERIFIABLE_CLAIM"
        and ev["reason_code"] == ReasonCode.UNVERIFIABLE_CLAIM
    )
    assert ev["claims_verbatim"] == [claim, "it was delivered to the wrong flat"]  # not summarized
