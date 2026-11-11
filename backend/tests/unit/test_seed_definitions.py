"""The seed definitions say what they claim. No database: this reads the definitions, the
policy corpus and the pinned clock, so a later edit that breaks (say) case 8 fails here, loudly.

Policy numbers (ceilings, window lengths, cooling-off, proration days) are read from the real
policy text rather than hardcoded, so the seed is checked against the policy it is meant to
exercise. Day counts use FixedClock() = SIMULATED_TODAY, never the real date.
"""

import re
from datetime import date, datetime, timedelta

from app.clock import SIMULATED_TODAY, FixedClock
from app.retrieval.policy_loader import PolicyDocument, load_policies
from app.seed import ids
from app.seed.definitions import (
    CARRIER,
    CASES,
    CUSTOMERS,
    ORDERS,
    PRODUCTS,
    REQUIRED_CASE_KEYS,
    SUBSCRIPTIONS,
    OrderDef,
    SubscriptionDef,
)

TODAY = FixedClock().today()
POLICIES = load_policies()
ORDER = {o.key: o for o in ORDERS}
SUB = {s.key: s for s in SUBSCRIPTIONS}
PRODUCT = {p.sku: p for p in PRODUCTS}


# --- helpers that read the real policy text ---------------------------------------------


def _doc(policy_type: str, version: int) -> PolicyDocument:
    return next(d for d in POLICIES if d.policy_type == policy_type and d.version == version)


def version_on(policy_type: str, day: date) -> int:
    """The policy version in effect on `day` (the purchase / start date)."""
    found = [
        d.version
        for d in POLICIES
        if d.policy_type == policy_type
        and d.valid_from <= day
        and (d.valid_to_exclusive is None or day < d.valid_to_exclusive)
    ]
    assert len(found) == 1, (policy_type, day, found)
    return found[0]


def _clause_number(doc: PolicyDocument, needle: str, pattern: str) -> int:
    for c in doc.clauses:
        if needle in c.text:
            m = re.search(pattern, c.text)
            assert m, (doc.source_path, c.text)
            return int(m.group(1))
    raise AssertionError(f"{doc.source_path}: no clause containing {needle!r}")


def refund_window_days(version: int) -> int:
    return _clause_number(_doc("refund", version), "window", r"(\d+) days")


def refund_ceiling(version: int) -> int:
    return _clause_number(_doc("refund", version), "Auto-approve ceiling", r"(\d+) cents")


def cooling_off_days() -> int:
    return _clause_number(_doc("cancellation", 2), "cooling-off", r"(\d+)-day cooling-off")


def proration_days() -> int:
    # Stated in v1 clause 3; v2 clause 1 inherits it ("As v1, but the fee is removed").
    return _clause_number(_doc("cancellation", 1), "prorated refund", r"first (\d+) days")


def presumed_lost_business_days() -> int:
    return _clause_number(_doc("shipping", 1), "presumed lost", r"(\d+) business days")


def add_business_days(start: date, n: int) -> date:
    """Monday to Friday, no holiday calendar (SPEC 16.4). Local to this test until
    app/domain/rules/business_days.py exists; replace this with that module then."""
    d = start
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


# --- derived facts about the seed ---------------------------------------------------------


def purchase_date(o: OrderDef) -> date:
    return o.placed_at.date()  # placed_at is UTC; the DB's purchase_date is the UTC date


def refund_version(o: OrderDef) -> int:
    return version_on("refund", purchase_date(o))


def sub_version(s: SubscriptionDef) -> int:
    return version_on("cancellation", s.started_at.date())


def day_of_subscription(s: SubscriptionDef) -> int:
    return (TODAY - s.first_charge_at.date()).days


def presumed_lost_date(o: OrderDef) -> date:
    assert o.shipment is not None
    return add_business_days(o.shipment.shipped_at.date(), presumed_lost_business_days())


def window_anchor(o: OrderDef) -> date | None:
    """Delivery date, or the presumed-lost date if undelivered and already past it."""
    assert o.shipment is not None
    if o.shipment.delivered_at is not None:
        return o.shipment.delivered_at.date()
    lost = presumed_lost_date(o)
    return lost if lost <= TODAY else None


def days_since_anchor(o: OrderDef) -> int:
    anchor = window_anchor(o)
    assert anchor is not None
    return (TODAY - anchor).days


def refundable_cents(o: OrderDef) -> int:
    """Sum paid for lines the refund policy allows: not final_sale, not an accessed add-on."""
    return sum(i.paid_cents for i in o.items if not i.final_sale and i.digital_accessed_at is None)


def window_open(o: OrderDef) -> bool:
    return days_since_anchor(o) <= refund_window_days(refund_version(o))


# --- structure ----------------------------------------------------------------------------


def test_the_clock_is_pinned_to_the_simulated_date() -> None:
    assert TODAY == SIMULATED_TODAY == date(2026, 4, 15)


def test_all_eight_required_cases_exist_with_stable_keys() -> None:
    assert sorted(REQUIRED_CASE_KEYS) == [1, 2, 3, 4, 5, 6, 7, 8]
    for number, key in REQUIRED_CASE_KEYS.items():
        case = next(c for c in CASES if c.key == key)
        assert case.number == number
        assert key in (ORDER if case.kind == "order" else SUB), (number, key)


def test_case_keys_and_row_ids_are_unique_and_deterministic() -> None:
    keys = [c.key for c in CASES]
    assert len(keys) == len(set(keys))
    assert len({o.key for o in ORDERS}) == len(ORDERS)
    assert len({o.order_number for o in ORDERS}) == len(ORDERS)
    assert len({s.key for s in SUBSCRIPTIONS}) == len(SUBSCRIPTIONS)
    # same key, same id, every time; different kinds never collide
    assert ids.order_id("final_sale_item_in_order") == ids.order_id("final_sale_item_in_order")
    assert str(ids.order_id("final_sale_item_in_order")) == str(
        ids.order_id("final_sale_item_in_order")
    )
    assert ids.order_id("x") != ids.subscription_id("x")
    every = [ids.order_id(o.key) for o in ORDERS] + [
        ids.subscription_id(s.key) for s in SUBSCRIPTIONS
    ]
    assert len(set(every)) == len(every)


def test_every_declared_case_resolves_to_a_definition() -> None:
    for case in CASES:
        assert case.key in (ORDER if case.kind == "order" else SUB)


def test_at_least_two_customers_each_with_their_own_data() -> None:
    assert len(CUSTOMERS) >= 2
    owners = {o.customer for o in ORDERS} | {s.customer for s in SUBSCRIPTIONS}
    assert owners == {c.key for c in CUSTOMERS}
    for c in CUSTOMERS:
        assert any(o.customer == c.key for o in ORDERS), c.key


def test_references_never_cross_customers() -> None:
    for o in ORDERS:
        if o.subscription:
            assert SUB[o.subscription].customer == o.customer, o.key
        if o.replacement_for:
            assert ORDER[o.replacement_for].customer == o.customer, o.key


def test_replacement_orders_come_after_the_orders_they_replace() -> None:
    order_keys = [o.key for o in ORDERS]  # the loader inserts in this order
    for o in ORDERS:
        if o.replacement_for:
            assert order_keys.index(o.replacement_for) < order_keys.index(o.key)


def test_money_is_consistent_on_every_order() -> None:
    for o in ORDERS:
        assert o.paid_cents >= 0, o.key
        assert o.discount_cents <= o.list_total_cents, o.key
        # items carry what was actually paid, excluding shipping
        assert sum(i.paid_cents for i in o.items) == o.list_total_cents - o.discount_cents, o.key
        for i in o.items:
            assert i.paid_cents <= i.qty * i.unit_price_cents, (o.key, i.sku)
        # list total is the sum of unit prices
        assert sum(i.qty * i.unit_price_cents for i in o.items) == o.list_total_cents, o.key


def test_orders_use_catalog_prices_except_where_discounted_away() -> None:
    for o in ORDERS:
        for i in o.items:
            assert i.unit_price_cents == PRODUCT[i.sku].price_cents, (o.key, i.sku)


def test_nothing_in_the_seed_happens_after_the_simulated_today() -> None:
    def past(d: datetime) -> bool:
        return d.date() <= TODAY

    for o in ORDERS:
        assert past(o.placed_at), o.key
        if o.shipment:
            assert past(o.shipment.shipped_at), o.key
            if o.shipment.delivered_at:
                assert past(o.shipment.delivered_at), o.key
            assert all(past(e.at) for e in o.shipment.events), o.key
        for i in o.items:
            if i.digital_accessed_at:
                assert past(i.digital_accessed_at), o.key
    for s in SUBSCRIPTIONS:
        assert past(s.started_at) and past(s.first_charge_at), s.key


def test_ship_dates_are_business_days_so_business_day_arithmetic_is_meaningful() -> None:
    for o in ORDERS:
        if o.shipment:
            assert o.shipment.shipped_at.weekday() < 5, (o.key, o.shipment.shipped_at)


def test_every_customer_facing_string_says_simulated() -> None:
    for c in CUSTOMERS:
        assert "SIMULATED" in c.display_name and c.email.endswith("@simulated.example")
    for p in PRODUCTS:
        assert "SIMULATED" in p.name, p.sku
    for o in ORDERS:
        assert o.order_number.startswith("SIM-"), o.key
        if o.shipment:
            assert o.shipment.tracking_no.startswith("SIM-TRK-"), o.key
            for e in o.shipment.events:
                assert "SIMULATED" in e.detail, (o.key, e.code)
    assert "SIMULATED" in CARRIER


def test_both_policy_versions_are_exercised_by_orders_and_subscriptions() -> None:
    assert {refund_version(o) for o in ORDERS} == {1, 2}
    assert {sub_version(s) for s in SUBSCRIPTIONS} == {1, 2}
    # and orders sit on both sides of the cutover itself
    cutover = _doc("refund", 2).valid_from
    assert any(purchase_date(o) < cutover for o in ORDERS)
    assert any(purchase_date(o) >= cutover for o in ORDERS)


# --- the eight required cases -------------------------------------------------------------


def test_case_1_v1_refund_above_v1_ceiling_but_below_v2_ceiling_inside_window() -> None:
    o = ORDER[REQUIRED_CASE_KEYS[1]]
    assert refund_version(o) == 1
    amount = refundable_cents(o)
    assert refund_ceiling(1) < amount < refund_ceiling(2)  # 7500 < 9800 < 12000
    assert window_open(o)  # otherwise it would be denied, not routed to human review
    # the anchor is after the cutover while the version is v1: version by purchase, window by delivery
    anchor = window_anchor(o)
    assert anchor is not None and anchor >= _doc("refund", 2).valid_from


def test_case_2_promotional_order_where_list_and_paid_differ() -> None:
    o = ORDER[REQUIRED_CASE_KEYS[2]]
    assert o.discount_cents > 0
    assert o.paid_cents != o.list_total_cents
    assert refundable_cents(o) == o.list_total_cents - o.discount_cents < o.list_total_cents
    # every discounted line was paid less than its list price
    assert all(i.paid_cents < i.qty * i.unit_price_cents for i in o.items)
    assert window_open(o)


def test_case_3_accessed_digital_addon_beside_refundable_items() -> None:
    o = ORDER[REQUIRED_CASE_KEYS[3]]
    accessed = [i for i in o.items if i.digital_accessed_at is not None]
    assert accessed and all(PRODUCT[i.sku].is_digital_addon for i in accessed)
    others = [i for i in o.items if i.digital_accessed_at is None and not i.final_sale]
    assert len(others) >= 1 and all(not PRODUCT[i.sku].is_digital_addon for i in others)
    assert refundable_cents(o) == sum(i.paid_cents for i in others) < o.list_total_cents
    assert window_open(o)


def test_case_4_final_sale_item_beside_a_refundable_item() -> None:
    o = ORDER[REQUIRED_CASE_KEYS[4]]
    final = [i for i in o.items if i.final_sale]
    assert len(final) >= 1
    assert any(not i.final_sale for i in o.items)
    assert refundable_cents(o) < o.list_total_cents
    assert window_open(o)  # non-refundable because of the flag, not because the window closed


def test_case_5_presumed_lost_shipment_with_replacement_already_sent() -> None:
    o = ORDER[REQUIRED_CASE_KEYS[5]]
    assert o.shipment is not None and o.shipment.delivered_at is None
    lost = presumed_lost_date(o)
    assert lost == date(2026, 3, 25) and lost < TODAY  # presumed lost as of today
    assert window_open(o)
    replacements = [r for r in ORDERS if r.replacement_for == o.key]
    assert len(replacements) == 1
    r = replacements[0]
    assert r.shipment is not None
    assert r.placed_at.date() > lost  # sent after the original was presumed lost
    # the replacement has its OWN window, from its own ship date, and is not itself lost yet
    assert presumed_lost_date(r) > TODAY
    assert r.paid_cents == 0  # a free replacement: refunding it can never exceed what was paid


def test_case_6_annual_subscription_inside_the_proration_window() -> None:
    s = SUB[REQUIRED_CASE_KEYS[6]]
    assert s.plan_type == "annual"
    day = day_of_subscription(s)
    assert 0 <= day <= proration_days()  # 54 <= 60
    assert sub_version(s) == 1  # v1: proration carries the administrative fee
    assert day > cooling_off_days()  # cooling-off is not a factor here


def test_case_7_subscription_inside_the_v2_cooling_off_period() -> None:
    s = SUB[REQUIRED_CASE_KEYS[7]]
    assert sub_version(s) == 2
    assert 0 <= day_of_subscription(s) <= cooling_off_days()  # 7 <= 14
    assert s.plan_type == "monthly"  # proration is annual-only: cooling-off is the only rule


def test_case_8_annual_v2_on_day_10_where_cooling_off_and_proration_could_both_fire() -> None:
    s = SUB[REQUIRED_CASE_KEYS[8]]
    assert s.plan_type == "annual"
    assert sub_version(s) == 2
    day = day_of_subscription(s)
    assert day == 10
    assert day <= cooling_off_days()  # 14
    assert day <= proration_days()  # 60: BOTH rules could apply; cooling-off must win
    # The assertion that cooling-off wins belongs to the cancellation rules engine (SPEC 16.3,
    # which must fail if the evaluation order is reversed). This test guarantees the data still
    # presents the conflict, so that engine test cannot pass vacuously.
    assert s.first_charge_cents == s.price_cents  # the full charge is what cooling-off refunds


# --- supplementary cases ------------------------------------------------------------------


def test_supplementary_v1_order_delivered_after_cutover_is_outside_30_inside_45() -> None:
    o = ORDER["v1_order_delivered_after_cutover_window_expired"]
    assert refund_version(o) == 1
    d = days_since_anchor(o)
    assert refund_window_days(1) < d <= refund_window_days(2)  # 30 < 41 <= 45
    assert not window_open(o)


def test_supplementary_exception_order_is_not_yet_presumed_lost() -> None:
    o = ORDER["v2_delivery_exception_before_presumed_lost"]
    assert o.shipment is not None and o.shipment.status == "exception"
    assert presumed_lost_date(o) > TODAY
    assert refund_version(o) == 2


def test_supplementary_subscriptions_cover_the_remaining_windows() -> None:
    after_cooling = SUB["annual_v2_inside_proration_after_cooling_off"]
    assert sub_version(after_cooling) == 2
    assert cooling_off_days() < day_of_subscription(after_cooling) <= proration_days()
    outside = SUB["annual_v1_outside_proration_window"]
    assert sub_version(outside) == 1 and day_of_subscription(outside) > proration_days()


def test_a_v2_order_cannot_be_outside_its_45_day_window_at_the_pinned_date() -> None:
    """Documents a limit of SIMULATED_TODAY rather than a seed defect: the earliest possible v2
    delivery is the cutover day, and cutover + 45 days is after today. Window EXPIRY under v2
    must be exercised by advancing the clock (FixedClock.plus_days), not by seed data."""
    earliest_v2_anchor = _doc("refund", 2).valid_from
    assert earliest_v2_anchor + timedelta(days=refund_window_days(2)) >= TODAY
