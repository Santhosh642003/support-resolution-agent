"""Synthetic seed data (SIMULATED BUSINESS). Every date is an absolute, explicit literal.

Nothing here reads a clock or a random source. The simulated "current date" is
app.clock.SIMULATED_TODAY (2026-04-15); each case below is positioned relative to it, and
tests/unit/test_seed_definitions.py asserts those positions so an edit that quietly changes
what a case means fails loudly.

Cases are addressed by stable, descriptive KEYS (never by database id). Row ids are derived
deterministically from the key (see ids.py), so a test can name a case without touching the
database. Weekday facts used in the comments (2026-03-01 is a Sunday) were checked by tests.

Cutover: refund and cancellation policy v2 take effect 2026-03-01. The policy VERSION is chosen
by the order's purchase date (or the subscription's start date); the refund WINDOW is measured
from delivery, or from the presumed-lost date. See docs/SPEC.md 16.1.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime

SIMULATED_LABEL = "SIMULATED"
CARRIER = "SimCarrier (SIMULATED)"
# Auth is a later task; this is deliberately not a usable password hash.
PLACEHOLDER_PASSWORD_HASH = "SIMULATED-PLACEHOLDER-NOT-A-REAL-HASH"  # noqa: S105


def at(year: int, month: int, day: int, hour: int = 12) -> datetime:
    """An explicit absolute instant (UTC). Noon by default so the UTC calendar date is
    unambiguous in any nearby timezone."""
    return datetime(year, month, day, hour, tzinfo=UTC)


@dataclass(frozen=True)
class CustomerDef:
    key: str
    email: str
    display_name: str
    created_at: datetime


@dataclass(frozen=True)
class ProductDef:
    sku: str
    name: str
    price_cents: int
    is_digital_addon: bool = False
    category: str = "general"  # structured fact used to scope retrieval


@dataclass(frozen=True)
class ItemDef:
    sku: str
    qty: int
    unit_price_cents: int
    paid_cents: int  # actually paid for the line, after discount allocation
    final_sale: bool = False
    digital_accessed_at: datetime | None = None
    # Subscription box lines only (refund v1 clause 4): items in the box and items shipped so far.
    items_total: int | None = None
    items_shipped: int | None = None


@dataclass(frozen=True)
class EventDef:
    at: datetime
    code: str
    detail: str


@dataclass(frozen=True)
class ShipmentDef:
    tracking_no: str
    status: str
    shipped_at: datetime
    expected_at: datetime
    delivered_at: datetime | None = None
    events: tuple[EventDef, ...] = ()


@dataclass(frozen=True)
class OrderDef:
    key: str
    order_number: str
    customer: str
    placed_at: datetime
    status: str
    list_total_cents: int
    items: tuple[ItemDef, ...]
    discount_cents: int = 0
    shipping_cents: int = 0
    kind: str = "purchase"
    subscription: str | None = None  # subscription key (subscription_charge orders)
    replacement_for: str | None = None  # order key
    shipment: ShipmentDef | None = None

    @property
    def paid_cents(self) -> int:
        return self.list_total_cents - self.discount_cents + self.shipping_cents


@dataclass(frozen=True)
class SubscriptionDef:
    key: str
    customer: str
    plan_type: str  # 'monthly' | 'annual'
    started_at: datetime
    first_charge_at: datetime
    first_charge_cents: int
    price_cents: int
    current_period_start: datetime
    current_period_end: datetime
    status: str = "active"


@dataclass(frozen=True)
class SeedCase:
    """A named scenario. `number` is the PM's required-case number (1-8), None for supplementary
    cases that exercise the same rules from the other side."""

    key: str
    kind: str  # 'order' | 'subscription'
    number: int | None
    summary: str = field(default="")


# --- customers ---------------------------------------------------------------------------

CUSTOMERS: tuple[CustomerDef, ...] = (
    CustomerDef("alex", "alex.rivera@simulated.example", "Alex Rivera (SIMULATED)", at(2025, 6, 2)),
    CustomerDef("blair", "blair.chen@simulated.example", "Blair Chen (SIMULATED)", at(2025, 6, 3)),
    CustomerDef("carol", "carol.osei@simulated.example", "Carol Osei (SIMULATED)", at(2025, 6, 4)),
)

# --- products ----------------------------------------------------------------------------

PRODUCTS: tuple[ProductDef, ...] = (
    ProductDef("SIM-BOX-MINI", "SIMULATED Mini Snack Box", 2200),
    ProductDef("SIM-BOX-CLASSIC", "SIMULATED Classic Snack Box", 3000),
    ProductDef("SIM-BOX-FAMILY", "SIMULATED Family Snack Box", 4800),
    ProductDef("SIM-BOX-DELUXE", "SIMULATED Deluxe Snack Box", 5000),
    ProductDef(
        "SIM-ADDON-RECIPES",
        "SIMULATED Digital Recipe Library (add-on)",
        1500,
        True,
        "digital_addon",
    ),
    ProductDef("SIM-CLEARANCE-MUG", "SIMULATED Clearance Mug (final sale)", 2000),
    ProductDef("SIM-PLAN-MONTHLY", "SIMULATED Monthly Plan", 1500, category="plan"),
    ProductDef("SIM-GIFTCARD-50", "SIMULATED Gift Card (50.00)", 5000, category="gift_card"),
    ProductDef("SIM-PLAN-ANNUAL", "SIMULATED Annual Plan", 12000, category="plan"),
)

# --- subscriptions -----------------------------------------------------------------------
# Day N = SIMULATED_TODAY minus the first-charge date, in calendar days.

SUBSCRIPTIONS: tuple[SubscriptionDef, ...] = (
    # CASE 6. v1 by start date. Day 54: inside the 60-day proration window (6 days left), past
    # any cooling-off (v1 has none). v1 proration carries the 10 percent administrative fee.
    SubscriptionDef(
        "annual_v1_inside_proration_window",
        "blair",
        "annual",
        at(2026, 2, 20),
        at(2026, 2, 20),
        12000,
        12000,
        at(2026, 2, 20),
        at(2027, 2, 20),
    ),
    # CASE 7. v2 by start date. Monthly plan, day 7: inside the 14-day cooling-off period, and
    # proration is annual-only, so cooling-off is the only rule that can apply.
    SubscriptionDef(
        "monthly_v2_inside_cooling_off",
        "carol",
        "monthly",
        at(2026, 4, 8),
        at(2026, 4, 8),
        1500,
        1500,
        at(2026, 4, 8),
        at(2026, 5, 8),
    ),
    # CASE 8. v2 by start date, annual, day 10: inside the 14-day cooling-off period AND inside
    # the 60-day proration window. Both could fire; cooling-off must win (SPEC 16.3).
    SubscriptionDef(
        "annual_v2_day10_cooling_off_beats_proration",
        "alex",
        "annual",
        at(2026, 4, 5),
        at(2026, 4, 5),
        12000,
        12000,
        at(2026, 4, 5),
        at(2027, 4, 5),
    ),
    # Supplementary. v2, annual, day 36: past cooling-off (14), inside proration (60), no fee.
    SubscriptionDef(
        "annual_v2_inside_proration_after_cooling_off",
        "carol",
        "annual",
        at(2026, 3, 10),
        at(2026, 3, 10),
        12000,
        12000,
        at(2026, 3, 10),
        at(2027, 3, 10),
    ),
    # Supplementary. v1, annual, day 163: outside the 60-day proration window, so cancellation
    # takes effect at the end of the period with no refund.
    SubscriptionDef(
        "annual_v1_outside_proration_window",
        "blair",
        "annual",
        at(2025, 11, 3),
        at(2025, 11, 3),
        12000,
        12000,
        at(2025, 11, 3),
        at(2026, 11, 3),
    ),
)


def _charge_order(number: str, sub: SubscriptionDef, product_sku: str) -> OrderDef:
    """The first-charge order of a subscription (a refund of that charge attaches to it)."""
    return OrderDef(
        key=f"charge_{sub.key}",
        order_number=number,
        customer=sub.customer,
        placed_at=sub.first_charge_at,
        status="delivered",
        list_total_cents=sub.first_charge_cents,
        kind="subscription_charge",
        subscription=sub.key,
        items=(ItemDef(product_sku, 1, sub.first_charge_cents, sub.first_charge_cents),),
    )


_SUB = {s.key: s for s in SUBSCRIPTIONS}

# --- orders ------------------------------------------------------------------------------

ORDERS: tuple[OrderDef, ...] = (
    # ===== Alex =====
    # CASE 1. v1 by purchase date (2026-02-26). Backordered, so it shipped 03-13 and was
    # delivered 03-18: the window anchor is AFTER the cutover while the policy version is v1
    # (SPEC 16.1). Day 28 since delivery: inside v1's 30-day window (2 days left). Refundable
    # amount 9800 is above the v1 ceiling (7500) and below the v2 ceiling (12000): human review
    # under v1, which v2 would have auto-approved.
    OrderDef(
        key="refund_v1_above_ceiling_human_review",
        order_number="SIM-1001",
        customer="alex",
        placed_at=at(2026, 2, 26),
        status="delivered",
        list_total_cents=9800,
        items=(
            ItemDef("SIM-BOX-DELUXE", 1, 5000, 5000),
            ItemDef("SIM-BOX-FAMILY", 1, 4800, 4800),
        ),
        shipment=ShipmentDef(
            "SIM-TRK-1001",
            "delivered",
            at(2026, 3, 13),
            at(2026, 3, 24),
            at(2026, 3, 18),
            (
                EventDef(at(2026, 3, 12), "LABEL_CREATED", "SIMULATED: backordered item released"),
                EventDef(at(2026, 3, 13), "PICKED_UP", "SIMULATED carrier pickup"),
                EventDef(at(2026, 3, 18), "DELIVERED", "SIMULATED delivery"),
            ),
        ),
    ),
    # CASE 2. v2 (2026-03-20). Promotional discount: list 8000, discount 2000, paid 6500 (with
    # 500 shipping), so the refundable base is what was paid, never list price. Delivered 03-27;
    # day 19 of the 45-day window.
    OrderDef(
        key="promo_discount_paid_differs_from_list",
        order_number="SIM-1002",
        customer="alex",
        placed_at=at(2026, 3, 20),
        status="delivered",
        list_total_cents=8000,
        discount_cents=2000,
        shipping_cents=500,
        items=(
            ItemDef("SIM-BOX-DELUXE", 1, 5000, 3750),
            ItemDef("SIM-BOX-CLASSIC", 1, 3000, 2250),
        ),
        shipment=ShipmentDef(
            "SIM-TRK-1002",
            "delivered",
            at(2026, 3, 23),
            at(2026, 4, 1),
            at(2026, 3, 27),
            (
                EventDef(at(2026, 3, 23), "PICKED_UP", "SIMULATED carrier pickup"),
                EventDef(at(2026, 3, 27), "DELIVERED", "SIMULATED delivery"),
            ),
        ),
    ),
    # CASE 3. v2 (2026-03-24). The digital add-on was accessed 03-30, so it is non-refundable;
    # the two physical boxes remain refundable. Delivered 03-31; day 15 of 45.
    OrderDef(
        key="digital_addon_accessed_with_refundable_items",
        order_number="SIM-1003",
        customer="alex",
        placed_at=at(2026, 3, 24),
        status="delivered",
        list_total_cents=9500,
        items=(
            ItemDef("SIM-BOX-DELUXE", 1, 5000, 5000),
            ItemDef("SIM-BOX-CLASSIC", 1, 3000, 3000),
            ItemDef("SIM-ADDON-RECIPES", 1, 1500, 1500, digital_accessed_at=at(2026, 3, 30)),
        ),
        shipment=ShipmentDef(
            "SIM-TRK-1003",
            "delivered",
            at(2026, 3, 25),
            at(2026, 4, 3),
            at(2026, 3, 31),
            (
                EventDef(at(2026, 3, 25), "PICKED_UP", "SIMULATED carrier pickup"),
                EventDef(at(2026, 3, 31), "DELIVERED", "SIMULATED delivery"),
            ),
        ),
    ),
    # CASE 4. v2 (2026-03-28). The clearance mug is final_sale: non-refundable regardless of
    # window. The classic box is refundable. Delivered 04-03; day 12 of 45.
    OrderDef(
        key="final_sale_item_in_order",
        order_number="SIM-1004",
        customer="alex",
        placed_at=at(2026, 3, 28),
        status="delivered",
        list_total_cents=5000,
        shipping_cents=500,
        items=(
            ItemDef("SIM-BOX-CLASSIC", 1, 3000, 3000),
            ItemDef("SIM-CLEARANCE-MUG", 1, 2000, 2000, final_sale=True),
        ),
        shipment=ShipmentDef(
            "SIM-TRK-1004",
            "delivered",
            at(2026, 3, 30),
            at(2026, 4, 8),
            at(2026, 4, 3),
            (
                EventDef(at(2026, 3, 30), "PICKED_UP", "SIMULATED carrier pickup"),
                EventDef(at(2026, 4, 3), "DELIVERED", "SIMULATED delivery"),
            ),
        ),
    ),
    # Supplementary. v1 by purchase date (2026-02-25), delivered 03-05 (after the cutover).
    # Day 41 since delivery: OUTSIDE v1's 30-day window, but it would be INSIDE a 45-day window.
    # The outcome therefore depends on choosing the version by purchase date, not delivery date.
    OrderDef(
        key="v1_order_delivered_after_cutover_window_expired",
        order_number="SIM-1005",
        customer="alex",
        placed_at=at(2026, 2, 25),
        status="delivered",
        list_total_cents=5200,
        shipping_cents=500,
        items=(
            ItemDef("SIM-BOX-MINI", 1, 2200, 2200),
            ItemDef("SIM-BOX-CLASSIC", 1, 3000, 3000),
        ),
        shipment=ShipmentDef(
            "SIM-TRK-1005",
            "delivered",
            at(2026, 2, 27),
            at(2026, 3, 10),
            at(2026, 3, 5),
            (
                EventDef(at(2026, 2, 27), "PICKED_UP", "SIMULATED carrier pickup"),
                EventDef(at(2026, 3, 5), "DELIVERED", "SIMULATED delivery"),
            ),
        ),
    ),
    # ===== Blair =====
    # CASE 5 (original). v2 (2026-03-10). Shipped 03-11, expected 03-20, still undelivered with
    # no carrier scan since 03-13. Presumed lost on 2026-03-25 (10 business days after ship).
    # Presumed-lost date is the window anchor: day 21 of the 45-day window.
    OrderDef(
        key="lost_shipment_with_replacement_sent",
        order_number="SIM-2001",
        customer="blair",
        placed_at=at(2026, 3, 10),
        status="shipped",
        list_total_cents=7000,
        items=(
            ItemDef("SIM-BOX-FAMILY", 1, 4800, 4800),
            ItemDef("SIM-BOX-MINI", 1, 2200, 2200),
        ),
        shipment=ShipmentDef(
            "SIM-TRK-2001",
            "in_transit",
            at(2026, 3, 11),
            at(2026, 3, 20),
            None,
            (
                EventDef(at(2026, 3, 10), "LABEL_CREATED", "SIMULATED label created"),
                EventDef(at(2026, 3, 11), "PICKED_UP", "SIMULATED carrier pickup"),
                EventDef(at(2026, 3, 13), "ARRIVED_HUB", "SIMULATED hub scan; no later scans"),
            ),
        ),
    ),
    # CASE 5 (replacement). Sent 04-06 for the lost shipment, at no charge (paid 0). It has its
    # OWN window, measured from its own ship date: expected 04-15, presumed lost 04-20, so as of
    # SIMULATED_TODAY it is in transit and not lost.
    OrderDef(
        key="replacement_in_transit",
        order_number="SIM-2002",
        customer="blair",
        placed_at=at(2026, 4, 3),
        status="shipped",
        list_total_cents=7000,
        discount_cents=7000,
        replacement_for="lost_shipment_with_replacement_sent",
        items=(
            ItemDef("SIM-BOX-FAMILY", 1, 4800, 0),
            ItemDef("SIM-BOX-MINI", 1, 2200, 0),
        ),
        shipment=ShipmentDef(
            "SIM-TRK-2002",
            "in_transit",
            at(2026, 4, 6),
            at(2026, 4, 15),
            None,
            (
                EventDef(at(2026, 4, 3), "LABEL_CREATED", "SIMULATED replacement label created"),
                EventDef(at(2026, 4, 6), "PICKED_UP", "SIMULATED carrier pickup"),
                EventDef(at(2026, 4, 9), "ARRIVED_HUB", "SIMULATED hub scan"),
            ),
        ),
    ),
    # Supplementary. v2 (2026-04-10), in transit and on schedule: expected 04-22.
    OrderDef(
        key="in_transit_on_schedule",
        order_number="SIM-2003",
        customer="blair",
        placed_at=at(2026, 4, 10),
        status="shipped",
        list_total_cents=3000,
        shipping_cents=500,
        items=(ItemDef("SIM-BOX-CLASSIC", 1, 3000, 3000),),
        shipment=ShipmentDef(
            "SIM-TRK-2003",
            "in_transit",
            at(2026, 4, 13),
            at(2026, 4, 22),
            None,
            (
                EventDef(at(2026, 4, 10), "LABEL_CREATED", "SIMULATED label created"),
                EventDef(at(2026, 4, 13), "PICKED_UP", "SIMULATED carrier pickup"),
                EventDef(at(2026, 4, 14), "ARRIVED_HUB", "SIMULATED hub scan"),
            ),
        ),
    ),
    # ===== Carol =====
    # Supplementary. v2 (2026-04-01), shipped 04-02, carrier delivery exception on 04-09. NOT yet
    # presumed lost (that date is 2026-04-16, one day after SIMULATED_TODAY), but v2 clause 3
    # permits a lost-shipment refund on the exception without waiting.
    OrderDef(
        key="v2_delivery_exception_before_presumed_lost",
        order_number="SIM-3001",
        customer="carol",
        placed_at=at(2026, 4, 1),
        status="shipped",
        list_total_cents=5000,
        shipping_cents=500,
        items=(ItemDef("SIM-BOX-DELUXE", 1, 5000, 5000),),
        shipment=ShipmentDef(
            "SIM-TRK-3001",
            "exception",
            at(2026, 4, 2),
            at(2026, 4, 13),
            None,
            (
                EventDef(at(2026, 4, 1), "LABEL_CREATED", "SIMULATED label created"),
                EventDef(at(2026, 4, 2), "PICKED_UP", "SIMULATED carrier pickup"),
                EventDef(
                    at(2026, 4, 9),
                    "DELIVERY_EXCEPTION",
                    "SIMULATED carrier delivery exception: address could not be verified",
                ),
            ),
        ),
    ),
    # Supplementary. v2 (2026-03-18). A SIMULATED gift card purchase: the only seeded order whose
    # structured facts put the gift card policy in retrieval scope (SPEC 7.3). Digital: no shipment.
    OrderDef(
        key="gift_card_purchase",
        order_number="SIM-3002",
        customer="carol",
        placed_at=at(2026, 3, 18),
        status="delivered",
        list_total_cents=5000,
        items=(ItemDef("SIM-GIFTCARD-50", 1, 5000, 5000),),
    ),
    # Supplementary. v2 (2026-03-26). A subscription box of 3 items of which 1 has shipped (this is
    # the first drop; delivered 04-04, day 11 of 45). Refund v1 clause 4, carried into v2 by v2
    # clause 6, refunds the unshipped share, rounded UP in the customer's favour: 5000 * 2 / 3 =
    # 3333.33 -> 3334 (rounding down would give 3333). The 500 shipping line is NOT refundable: the
    # shipment arrived. This is the one seeded case that makes the proration rule fire end to end.
    OrderDef(
        key="partially_shipped_subscription_box",
        order_number="SIM-3003",
        customer="carol",
        placed_at=at(2026, 3, 26),
        status="delivered",
        list_total_cents=5000,
        shipping_cents=500,
        items=(ItemDef("SIM-BOX-DELUXE", 1, 5000, 5000, items_total=3, items_shipped=1),),
        shipment=ShipmentDef(
            "SIM-TRK-3003",
            "delivered",
            at(2026, 3, 31),
            at(2026, 4, 9),
            at(2026, 4, 4),
            (
                EventDef(at(2026, 3, 31), "PICKED_UP", "SIMULATED carrier pickup"),
                EventDef(at(2026, 4, 4), "DELIVERED", "SIMULATED delivery"),
            ),
        ),
    ),
    # Supplementary. The chain "lost, replaced, replacement lost, refund available" (shipping v1
    # clause 4), end to end. v2 (2026-03-02). Shipped 03-03, no scan after 03-05: presumed lost
    # 2026-03-17 (10 business days), day 29 of the 45-day window. The replacement SIM-3005 (below)
    # shipped 03-23 and is itself presumed lost on 2026-04-06, so as of SIMULATED_TODAY it is no
    # longer the remedy and the original is refundable: 3000 for the box plus the 500 shipping line.
    OrderDef(
        key="lost_shipment_replacement_also_lost",
        order_number="SIM-3004",
        customer="carol",
        placed_at=at(2026, 3, 2),
        status="shipped",
        list_total_cents=3000,
        shipping_cents=500,
        items=(ItemDef("SIM-BOX-CLASSIC", 1, 3000, 3000),),
        shipment=ShipmentDef(
            "SIM-TRK-3004",
            "in_transit",
            at(2026, 3, 3),
            at(2026, 3, 12),
            None,
            (
                EventDef(at(2026, 3, 2), "LABEL_CREATED", "SIMULATED label created"),
                EventDef(at(2026, 3, 3), "PICKED_UP", "SIMULATED carrier pickup"),
                EventDef(at(2026, 3, 5), "ARRIVED_HUB", "SIMULATED hub scan; no later scans"),
            ),
        ),
    ),
    # The replacement for SIM-3004, at no charge (paid 0), with its OWN window: shipped 03-23,
    # presumed lost 2026-04-06, so it is lost as of SIMULATED_TODAY (04-15).
    OrderDef(
        key="replacement_also_lost",
        order_number="SIM-3005",
        customer="carol",
        placed_at=at(2026, 3, 20),
        status="shipped",
        list_total_cents=3000,
        discount_cents=3000,
        replacement_for="lost_shipment_replacement_also_lost",
        items=(ItemDef("SIM-BOX-CLASSIC", 1, 3000, 0),),
        shipment=ShipmentDef(
            "SIM-TRK-3005",
            "in_transit",
            at(2026, 3, 23),
            at(2026, 4, 1),
            None,
            (
                EventDef(at(2026, 3, 20), "LABEL_CREATED", "SIMULATED replacement label created"),
                EventDef(at(2026, 3, 23), "PICKED_UP", "SIMULATED carrier pickup"),
                EventDef(at(2026, 3, 25), "ARRIVED_HUB", "SIMULATED hub scan; no later scans"),
            ),
        ),
    ),
    # First-charge orders for each subscription.
    _charge_order("SIM-S-0001", _SUB["annual_v1_inside_proration_window"], "SIM-PLAN-ANNUAL"),
    _charge_order("SIM-S-0002", _SUB["monthly_v2_inside_cooling_off"], "SIM-PLAN-MONTHLY"),
    _charge_order(
        "SIM-S-0003", _SUB["annual_v2_day10_cooling_off_beats_proration"], "SIM-PLAN-ANNUAL"
    ),
    _charge_order(
        "SIM-S-0004", _SUB["annual_v2_inside_proration_after_cooling_off"], "SIM-PLAN-ANNUAL"
    ),
    _charge_order("SIM-S-0005", _SUB["annual_v1_outside_proration_window"], "SIM-PLAN-ANNUAL"),
)

# --- the named cases ---------------------------------------------------------------------

CASES: tuple[SeedCase, ...] = (
    SeedCase(
        "refund_v1_above_ceiling_human_review",
        "order",
        1,
        "v1 order, refundable 9800 is above the 7500 v1 ceiling and below the 12000 v2 ceiling",
    ),
    SeedCase(
        "promo_discount_paid_differs_from_list",
        "order",
        2,
        "promotional discount: list 8000, paid 6500",
    ),
    SeedCase(
        "digital_addon_accessed_with_refundable_items",
        "order",
        3,
        "accessed digital add-on beside refundable boxes",
    ),
    SeedCase("final_sale_item_in_order", "order", 4, "final_sale item beside a refundable box"),
    SeedCase(
        "lost_shipment_with_replacement_sent",
        "order",
        5,
        "presumed-lost shipment, replacement already sent",
    ),
    SeedCase(
        "annual_v1_inside_proration_window",
        "subscription",
        6,
        "annual v1 plan on day 54: inside the 60-day proration window",
    ),
    SeedCase(
        "monthly_v2_inside_cooling_off",
        "subscription",
        7,
        "monthly v2 plan on day 7: inside the 14-day cooling-off period",
    ),
    SeedCase(
        "annual_v2_day10_cooling_off_beats_proration",
        "subscription",
        8,
        "annual v2 plan on day 10: cooling-off and proration both apply, cooling-off wins",
    ),
    # supplementary
    SeedCase(
        "v1_order_delivered_after_cutover_window_expired",
        "order",
        None,
        "v1 order delivered after the cutover; outside 30 days, inside 45",
    ),
    SeedCase("replacement_in_transit", "order", None, "the replacement for case 5"),
    SeedCase(
        "gift_card_purchase", "order", None, "a gift card purchase: gift card policy in scope"
    ),
    SeedCase(
        "partially_shipped_subscription_box",
        "order",
        None,
        "subscription box, 1 of 3 items shipped: proration rounds up, 3334 of 5000",
    ),
    SeedCase(
        "lost_shipment_replacement_also_lost",
        "order",
        None,
        "lost, replaced, and the replacement is lost too: the original is refundable",
    ),
    SeedCase("replacement_also_lost", "order", None, "the replacement for SIM-3004, itself lost"),
    SeedCase("in_transit_on_schedule", "order", None, "order-status lookup, on schedule"),
    SeedCase(
        "v2_delivery_exception_before_presumed_lost",
        "order",
        None,
        "carrier exception, one day before presumed lost",
    ),
    SeedCase(
        "annual_v2_inside_proration_after_cooling_off",
        "subscription",
        None,
        "annual v2 plan on day 36: proration, no fee",
    ),
    SeedCase(
        "annual_v1_outside_proration_window",
        "subscription",
        None,
        "annual v1 plan on day 163: outside proration",
    ),
)

REQUIRED_CASE_KEYS: dict[int, str] = {c.number: c.key for c in CASES if c.number is not None}
