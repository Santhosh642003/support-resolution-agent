"""Order-status determination: a pure function of ship / delivery dates and the injected clock.

Given a shipment, is it inside its delivery window, late, or presumed lost? The answer is computed
here, deterministically, from the shipping policy's numbers (`app/domain/rules/shipping.py`), and
the result RECORDS WHICH CLAUSES IT APPLIED. Those clause keys are the citations (SPEC 10.4); the
model only explains the result.

Applied clauses (shipping policy, one version today):
  clause 1  standard delivery window, 7 business days from the ship date: whenever a ship date
            exists (it fixes the expected delivery date and whether a delivery was in time)
  clause 2  presumed lost if undelivered 10 business days after the ship date: whenever the
            shipment is undelivered
  clause 3  a replacement shipment carries its own window from its own ship date: when the order is
            a replacement
DECISIVE vs SUPPORTING. Every applied clause is one or the other (SPEC 10.5). A clause is
decisive when it is what separates this outcome from its neighbour: had it been absent or
different, THIS outcome would not have been reached for the reason given. Delivered / on schedule /
late are decided by the delivery window (clause 1); presumed lost and a carrier exception are
decided by the presumed-lost rule (clause 2). The others applied are supporting: they locate the
dates or rule out a neighbouring outcome without being the reason (clause 2 for a late parcel,
clause 1 for a lost one, clause 3 for a replacement). The explanation must cite every decisive
clause.
No clause is applied when there is nothing to measure (not shipped yet, digital, canceled).

SAFE BY DEFAULT. The rule refuses to guess: a policy version it does not implement, or a product
category whose shipping timelines differ from the general window (hazardous, perishable, oversize,
age-restricted: shipping_restrictions policy), yields `needs_human` instead of a wrong answer. That
category conflict is an OPEN ITEM carried to the rules task (M2), not resolved here.

The determination authorises no action: its decision outcome is 'informational'.
"""

from dataclasses import dataclass, field, replace
from datetime import date
from enum import StrEnum

from app.domain.reason_codes import ReasonCode
from app.domain.rules.shipping import (
    SHIPPING_POLICY_VERSION,
    expected_delivery_date,
    presumed_lost_date,
)
from app.retrieval.policy_loader import chunk_key

RULE_SET_VERSION = "order-status-1"
RESTRICTED_CATEGORIES = frozenset({"hazardous", "perishable", "oversize", "age_restricted"})


class OrderStatusOutcome(StrEnum):
    DELIVERED = "delivered"
    IN_TRANSIT_ON_SCHEDULE = "in_transit_on_schedule"
    LATE = "late"
    PRESUMED_LOST = "presumed_lost"
    DELIVERY_EXCEPTION = "delivery_exception"
    NOT_SHIPPED = "not_shipped"
    CANCELED = "canceled"
    DELIVERED_DIGITAL = "delivered_digital"
    NEEDS_HUMAN = "needs_human"


_DECIDED_BY_PRESUMED_LOST_RULE = frozenset(
    {OrderStatusOutcome.PRESUMED_LOST, OrderStatusOutcome.DELIVERY_EXCEPTION}
)


@dataclass(frozen=True)
class ShipmentInput:
    status: str  # label_created | in_transit | delivered | exception | lost
    shipped_on: date | None
    delivered_on: date | None
    carrier_expected_on: date | None
    has_delivery_exception: bool


@dataclass(frozen=True)
class OrderStatusInput:
    order_number: str
    order_status: str  # placed | shipped | delivered | canceled
    purchase_date: date  # selects the policy version (never the window anchor)
    is_replacement: bool
    categories: frozenset[str]
    shipment: ShipmentInput | None


@dataclass(frozen=True)
class OrderStatusResult:
    outcome: OrderStatusOutcome
    reason_codes: tuple[ReasonCode, ...]
    applied_clauses: tuple[int, ...]  # clause numbers of the shipping policy
    policy_version: int
    facts: dict[str, str | int | bool | None] = field(default_factory=dict)
    decisive_clauses: tuple[int, ...] = ()  # the subset that determined the outcome

    @property
    def decision_outcome(self) -> str:
        return "needs_human" if self.outcome is OrderStatusOutcome.NEEDS_HUMAN else "informational"

    @property
    def decisive_clause_keys(self) -> tuple[str, ...]:
        return tuple(chunk_key("shipping", self.policy_version, n) for n in self.decisive_clauses)

    @property
    def supporting_clause_keys(self) -> tuple[str, ...]:
        return tuple(
            chunk_key("shipping", self.policy_version, n)
            for n in self.applied_clauses
            if n not in self.decisive_clauses
        )

    @property
    def applied_clause_keys(self) -> tuple[str, ...]:
        return tuple(chunk_key("shipping", self.policy_version, n) for n in self.applied_clauses)


def _iso(d: date | None) -> str | None:
    return None if d is None else d.isoformat()


def determine_order_status(
    inp: OrderStatusInput, today: date, policy_version: int
) -> OrderStatusResult:
    """`policy_version` is the shipping policy version IN EFFECT on the purchase date (looked up
    by the caller). `today` comes from the injected Clock; this function never reads one."""
    base: dict[str, str | int | bool | None] = {
        "order_number": inp.order_number,
        "as_of": today.isoformat(),
        "policy_version": policy_version,
    }

    def result(
        outcome: OrderStatusOutcome,
        reasons: list[ReasonCode],
        clauses: tuple[int, ...] = (),
        **facts: str | int | bool | None,
    ) -> OrderStatusResult:
        return OrderStatusResult(
            outcome, tuple(reasons), clauses, policy_version, {**base, **facts}
        )

    if inp.order_status == "canceled":
        return result(OrderStatusOutcome.CANCELED, [ReasonCode.ORDER_CANCELED])
    if policy_version != SHIPPING_POLICY_VERSION:
        return result(
            OrderStatusOutcome.NEEDS_HUMAN,
            [ReasonCode.POLICY_VERSION_NOT_IMPLEMENTED],
            implemented_version=SHIPPING_POLICY_VERSION,
        )
    ship = inp.shipment
    if inp.categories & RESTRICTED_CATEGORIES and (ship is None or ship.delivered_on is None):
        return result(
            OrderStatusOutcome.NEEDS_HUMAN,
            [ReasonCode.CATEGORY_TIMELINE_NOT_IMPLEMENTED],
            categories=",".join(sorted(inp.categories & RESTRICTED_CATEGORIES)),
        )
    if ship is None:
        if inp.order_status == "delivered":  # digital goods: delivered on purchase
            return result(OrderStatusOutcome.DELIVERED_DIGITAL, [ReasonCode.NO_PHYSICAL_SHIPMENT])
        return result(OrderStatusOutcome.NOT_SHIPPED, [ReasonCode.NO_SHIPMENT_YET])
    if ship.shipped_on is None:
        return result(OrderStatusOutcome.NOT_SHIPPED, [ReasonCode.NO_SHIPMENT_YET])

    expected = expected_delivery_date(ship.shipped_on)
    lost_on = presumed_lost_date(ship.shipped_on)
    facts: dict[str, str | int | bool | None] = {
        "shipped_on": _iso(ship.shipped_on),
        "delivered_on": _iso(ship.delivered_on),
        "expected_on": _iso(expected),
        "presumed_lost_on": _iso(lost_on),
        "carrier_status": ship.status,
        "is_replacement": inp.is_replacement,
    }
    clauses = [1]
    reasons: list[ReasonCode]
    outcome: OrderStatusOutcome

    if ship.delivered_on is not None:
        outcome = OrderStatusOutcome.DELIVERED
        in_time = ship.delivered_on <= expected
        reasons = [
            ReasonCode.DELIVERED,
            ReasonCode.DELIVERED_WITHIN_WINDOW if in_time else ReasonCode.DELIVERED_AFTER_WINDOW,
        ]
        facts["days_late"] = 0 if in_time else (ship.delivered_on - expected).days
    else:
        clauses.append(2)
        if ship.status == "lost" or today >= lost_on:
            outcome = OrderStatusOutcome.PRESUMED_LOST
            reasons = (
                [ReasonCode.CARRIER_REPORTED_LOST]
                if ship.status == "lost" and today < lost_on
                else [ReasonCode.PRESUMED_LOST, ReasonCode.UNDELIVERED_PAST_PRESUMED_LOST_DATE]
            )
        elif ship.has_delivery_exception or ship.status == "exception":
            outcome = OrderStatusOutcome.DELIVERY_EXCEPTION
            reasons = [ReasonCode.CARRIER_DELIVERY_EXCEPTION, ReasonCode.NOT_YET_PRESUMED_LOST]
        elif today > expected:
            outcome = OrderStatusOutcome.LATE
            reasons = [ReasonCode.PAST_DELIVERY_WINDOW, ReasonCode.NOT_YET_PRESUMED_LOST]
        else:
            outcome = OrderStatusOutcome.IN_TRANSIT_ON_SCHEDULE
            reasons = [ReasonCode.WITHIN_DELIVERY_WINDOW]
        facts["days_past_expected"] = max((today - expected).days, 0)
        facts["days_until_presumed_lost"] = max((lost_on - today).days, 0)
    if inp.is_replacement:
        clauses.append(3)
        reasons.append(ReasonCode.REPLACEMENT_OWN_WINDOW)
    decisive = (2,) if outcome in _DECIDED_BY_PRESUMED_LOST_RULE else (1,)
    return replace(result(outcome, reasons, tuple(clauses), **facts), decisive_clauses=decisive)
