"""The refund rules engine (M2 task 1): the seeded cases, every threshold from both sides, and the
two-anchor guard. No database, no model, no network. `today` is the pinned SIMULATED_TODAY.

Cases 6, 7 and 8 of the PM's eight are SUBSCRIPTION CANCELLATION cases (task 2). The refund engine
is only asserted to route their first-charge orders away (SUBSCRIPTION_CHARGE_USES_CANCELLATION_RULES);
cases 1 to 5 are exercised here in full, with the supplementary seeded orders from the other side.
"""

import re
from datetime import date, timedelta

import pytest

from app.clock import FixedClock
from app.domain.reason_codes import ReasonCode as R
from app.domain.rules import refund as refund_module
from app.domain.rules.refund import (
    TERMS,
    Claim,
    ClaimKind,
    RefundItem,
    RefundOrder,
    RefundOutcome,
    RefundShipment,
    decide_refund,
    select_policy_version,
)
from app.domain.types import PurchaseDate
from app.retrieval.policy_loader import PolicyDocument, load_policies
from tests.unit.refund_fixtures import ORDER, delivered_order, from_seed

TODAY = FixedClock().today()  # 2026-04-15
ALLOWED, DENIED, HUMAN = RefundOutcome.ALLOWED, RefundOutcome.DENIED, RefundOutcome.NEEDS_HUMAN
POLICIES = load_policies()
V1_DAY = date(2026, 2, 1)  # purchase dates that select v1 / v2
V2_DAY = date(2026, 4, 1)


def lines(d: object) -> list[int]:
    return [line.amount_cents for line in d.lines]  # type: ignore[attr-defined]


# --- the policy numbers in code are the numbers in the policy text -------------------------------


def _doc(version: int) -> PolicyDocument:
    return next(d for d in POLICIES if d.policy_type == "refund" and d.version == version)


@pytest.mark.parametrize("terms", TERMS, ids=lambda t: f"v{t.version}")
def test_the_terms_in_code_equal_the_stored_policy_text(terms: refund_module.RefundTerms) -> None:
    doc = _doc(terms.version)
    text = {c.number: c.text for c in doc.clauses}
    assert re.search(rf"\b{terms.window_days} days\b", text[terms.window]), text[terms.window]
    assert re.search(rf"\b{terms.ceiling_cents} cents\b", text[terms.ceiling]), text[terms.ceiling]
    assert doc.valid_from == terms.effective_from
    expected_to = None if doc.valid_to_exclusive is None else doc.valid_to_exclusive - timedelta(1)
    assert expected_to == terms.effective_to
    assert ("final_sale" in text[terms.final_sale]) or terms.version == 2  # v2: "unchanged"
    assert "Digital add-on" in text[terms.digital] or "Digital add-ons" in text[terms.digital]
    assert ("exception" in text[terms.exception]) if terms.exception else terms.version == 1
    assert "actually paid" in text[terms.promo_paid_clause] if terms.promo_paid_clause else True
    # The ceiling is cumulative per order in BOTH versions (SPEC 18.6, drafting correction).
    assert "cumulative" in text[terms.ceiling] and "prior refunds" in text[terms.ceiling]
    incorporated = {i.clause_number: set(i.targets) for i in doc.incorporations}
    if terms.lost_via:  # v2: the RESIDUAL clause states nothing itself; it points at v1
        assert "remains in effect" in text[terms.lost] and terms.lost == terms.proration
        assert set(terms.lost_via) | set(terms.proration_via) <= incorporated[terms.lost]
        assert terms.lost_via == ("refund-v1-clause-2",)
        # the anchor is stated by the window clause itself, NOT linked to v1's superseded 30 days
        assert "presumed-lost date" in text[terms.window]
        assert "refund-v1-clause-1" not in incorporated[terms.lost]
        assert terms.proration_via == ("refund-v1-clause-4",)
        assert not any("Lost or undelivered" in t for t in text.values())  # stated once, in v1
    else:
        assert "Lost or undelivered" in text[terms.lost] and "shipping charge" in text[terms.lost]
        assert "prorated" in text[terms.proration] and "rounded up" in text[terms.proration]


def test_every_clause_key_the_engine_can_cite_exists_in_the_corpus() -> None:
    keys = {d.chunk_key(c.number) for d in POLICIES for c in d.clauses}
    for t in TERMS:
        for n in (t.window, t.ceiling, t.digital, t.final_sale, t.lost, t.proration, t.exception):
            if n is not None:
                assert f"refund-v{t.version}-clause-{n}" in keys
        if t.promo_paid_clause:
            assert f"refund-v{t.version}-clause-{t.promo_paid_clause}" in keys
    for k in ("shipping-v1-clause-2", "shipping_restrictions-v1-clause-3",
              "shipping_restrictions-v1-clause-4", "shipping_restrictions-v1-clause-6"):  # fmt: skip
        assert k in keys


# --- the seeded cases ----------------------------------------------------------------------------


def test_case_1_above_the_v1_ceiling_is_human_review_not_denial() -> None:
    d = decide_refund(from_seed("refund_v1_above_ceiling_human_review"), TODAY)
    assert d.outcome is HUMAN and d.policy_version == 1
    assert lines(d) == [5000, 4800] and d.aggregate_cents == 9800
    assert d.authorized_cents == 0  # nothing may be refunded before a human approves
    assert d.reason_codes == (R.ABOVE_AUTO_APPROVE_CEILING, R.REFUND_WITHIN_WINDOW)
    assert d.decisive_clause_keys == ("refund-v1-clause-3",)
    assert d.supporting_clause_keys == ("refund-v1-clause-1",)
    assert d.facts["days_since_anchor"] == 28 and d.facts["days_left"] == 2
    assert d.handoff is not None and d.handoff.evidence["proposed_cents"] == 9800
    # The same facts under v2 are auto-approved, so the ceiling is what decides this case.
    assert 9800 <= TERMS[1].ceiling_cents


def test_case_2_promotional_discount_refunds_the_amount_paid_never_list_price() -> None:
    o = from_seed("promo_discount_paid_differs_from_list")
    assert o.list_total_cents == 8000 and o.paid_cents == 6500  # list and paid differ
    d = decide_refund(o, TODAY)
    assert d.outcome is ALLOWED and d.policy_version == 2
    assert lines(d) == [3750, 2250, 0] and d.aggregate_cents == d.authorized_cents == 6000
    assert d.aggregate_cents < o.list_total_cents - o.discount_cents + 1  # never list price
    assert d.reason_codes == (
        R.REFUND_WITHIN_WINDOW,
        R.SHIPPING_NOT_REFUNDABLE,  # the 500 shipping line: the shipment arrived
        R.PAID_AMOUNT_BASIS_POLICY,
    )
    assert d.decisive_clause_keys == (
        "refund-v2-clause-1",
        "refund-v2-clause-6",
        "refund-v2-clause-4",
    )
    assert d.supporting_clause_keys == ("refund-v2-clause-2",)


def test_case_3_an_accessed_digital_addon_is_excluded_the_boxes_are_not() -> None:
    d = decide_refund(from_seed("digital_addon_accessed_with_refundable_items"), TODAY)
    assert d.outcome is ALLOWED and d.policy_version == 2
    assert lines(d) == [5000, 3000, 0] and d.aggregate_cents == 8000
    assert d.lines[2].reason_codes == (R.DIGITAL_ADDON_ACCESSED,)
    assert d.reason_codes == (R.REFUND_WITHIN_WINDOW,)
    assert d.decisive_clause_keys == ("refund-v2-clause-1", "refund-v2-clause-5")
    assert d.supporting_clause_keys == ("refund-v2-clause-2",)


def test_case_4_a_final_sale_item_never_contributes_and_shipping_is_not_refunded() -> None:
    o = from_seed("final_sale_item_in_order")
    d = decide_refund(o, TODAY)
    assert d.outcome is ALLOWED
    assert lines(d) == [3000, 0, 0] and d.aggregate_cents == 3000  # paid 5500: mug 2000, ship 500
    assert o.paid_cents == 5500
    assert d.lines[1].reason_codes == (R.FINAL_SALE_EXCLUDED,)
    assert d.lines[2].reason_codes == (R.SHIPPING_NOT_REFUNDABLE,)  # delivered: shipping stays
    assert d.decisive_clause_keys == (
        "refund-v2-clause-1",
        "refund-v2-clause-5",
        "refund-v2-clause-6",
    )
    assert d.supporting_clause_keys == ("refund-v2-clause-2",)


def test_case_5_the_replacement_is_the_remedy_so_the_lost_original_is_denied() -> None:
    """SIM-2001 is presumed lost and SIM-2002 (the replacement) is in transit: refunding the original
    too would give the customer the goods and the money. Denied, naming the replacement and its OWN
    window (shipping v1 clause 4); not a handoff (SPEC 18.6)."""
    o = from_seed("lost_shipment_with_replacement_sent")
    assert [r.order_number for r in o.replacements] == ["SIM-2002"]
    d = decide_refund(o, TODAY)
    assert d.outcome is DENIED and d.policy_version == 2 and d.handoff is None
    assert lines(d) == [0, 0] and d.authorized_cents == 0
    assert d.reason_codes == (R.REPLACEMENT_IS_REMEDY,)
    assert d.facts["replacement_order"] == "SIM-2002"
    assert d.facts["replacement_shipped_on"] == "2026-04-06"
    assert (
        d.facts["replacement_presumed_lost_on"] == "2026-04-20"
    )  # its own window, not the original's
    assert d.facts["presumed_lost_on"] == "2026-03-25"
    assert d.decisive_clause_keys == ("shipping-v1-clause-4",)
    assert "shipping-v1-clause-3" in d.supporting_clause_keys


def test_supplementary_v1_order_delivered_after_the_cutover_is_outside_its_30_day_window() -> None:
    d = decide_refund(from_seed("v1_order_delivered_after_cutover_window_expired"), TODAY)
    assert d.outcome is DENIED and d.policy_version == 1
    assert d.facts["days_since_anchor"] == 41 and lines(d) == [0, 0, 0]
    assert d.reason_codes == (R.REFUND_WINDOW_EXPIRED,)
    assert d.decisive_clause_keys == ("refund-v1-clause-1",) and d.supporting_clause_keys == ()


def test_supplementary_a_delivery_exception_permits_the_early_refund_under_v2() -> None:
    d = decide_refund(from_seed("v2_delivery_exception_before_presumed_lost"), TODAY)
    assert d.outcome is ALLOWED and d.policy_version == 2
    # The whole shipment is undelivered, so the 500 shipping line is refunded too: 5500 of 5500 paid.
    assert lines(d) == [5000, 500] and d.authorized_cents == 5500
    assert d.facts["presumed_lost_on"] == "2026-04-16"  # one day away: "without waiting" matters
    assert d.reason_codes == (
        R.REFUND_WITHIN_WINDOW,
        R.CARRIER_DELIVERY_EXCEPTION,
        R.EARLY_REFUND_ON_DELIVERY_EXCEPTION,
        R.SHIPPING_CHARGE_REFUNDED,
    )
    assert d.decisive_clause_keys == (
        "refund-v2-clause-3",
        "refund-v2-clause-1",
        "refund-v2-clause-6",
    )
    assert d.supporting_clause_keys == ("shipping-v1-clause-2", "refund-v2-clause-2")


def test_the_same_exception_under_v1_does_not_shorten_the_wait() -> None:
    o = from_seed("v2_delivery_exception_before_presumed_lost")
    v1 = RefundOrder(**{**o.__dict__, "purchase_date": PurchaseDate(V1_DAY)})
    d = decide_refund(v1, TODAY)
    assert d.outcome is DENIED and d.policy_version == 1
    assert d.reason_codes == (
        R.NOT_YET_REFUNDABLE,
        R.CARRIER_DELIVERY_EXCEPTION,
        R.NOT_YET_PRESUMED_LOST,
    )
    assert d.facts["eligible_from"] == "2026-04-16" and d.aggregate_cents == 0
    assert d.decisive_clause_keys == ("shipping-v1-clause-2", "refund-v1-clause-2")


@pytest.mark.parametrize("key", ["replacement_in_transit", "in_transit_on_schedule"])
def test_supplementary_in_transit_shipments_are_not_yet_refundable(key: str) -> None:
    d = decide_refund(from_seed(key), TODAY)
    assert d.outcome is DENIED and d.aggregate_cents == 0
    assert d.reason_codes == (R.NOT_YET_REFUNDABLE, R.NOT_YET_PRESUMED_LOST)
    # v2 carries the lost-shipment rule through its residual clause 6 (incorporating v1 clause 2).
    assert d.decisive_clause_keys == ("shipping-v1-clause-2", "refund-v2-clause-6")


def test_a_purchase_with_no_shipment_has_no_window_to_measure() -> None:
    d = decide_refund(from_seed("gift_card_purchase"), TODAY)  # digital, delivered, no shipment
    assert d.outcome is HUMAN and d.reason_codes == (R.NO_WINDOW_ANCHOR,) and d.applied == ()


@pytest.mark.parametrize(
    "number", ["SIM-S-0001", "SIM-S-0002", "SIM-S-0003", "SIM-S-0004", "SIM-S-0005"]
)
def test_subscription_charge_orders_belong_to_the_cancellation_rules(number: str) -> None:
    """Seed cases 6, 7 and 8 (and the supplementary subscriptions) are cancellation cases, task 2."""
    d = decide_refund(from_seed(number), TODAY)
    assert d.outcome is HUMAN and d.aggregate_cents == 0
    assert d.reason_codes == (R.SUBSCRIPTION_CHARGE_USES_CANCELLATION_RULES,)


def test_every_seeded_order_gets_a_decision_with_the_sum_invariant() -> None:
    for o in ORDER.values():
        d = decide_refund(from_seed(o.key), TODAY)
        assert d.aggregate_cents == sum(lines(d))
        assert d.authorized_cents in (0, d.aggregate_cents)
        assert set(d.decisive_clause_keys) <= set(d.applied_clause_keys)


# --- the version cutover: 2026-02-28, 2026-03-01, 2026-03-02 -------------------------------------


@pytest.mark.parametrize(
    ("purchase", "version"),
    [(date(2026, 2, 28), 1), (date(2026, 3, 1), 2), (date(2026, 3, 2), 2)],
)
def test_version_cutover_is_selected_by_purchase_date(purchase: date, version: int) -> None:
    terms = select_policy_version(PurchaseDate(purchase))
    assert terms is not None and terms.version == version
    o = delivered_order(purchase, TODAY - timedelta(days=10), 4000)
    assert decide_refund(o, TODAY).policy_version == version


def test_the_policy_start_edges_and_the_days_before_it() -> None:
    assert select_policy_version(PurchaseDate(date(2025, 6, 1))).version == 1  # type: ignore[union-attr]
    assert select_policy_version(PurchaseDate(date(2025, 5, 31))) is None
    o = delivered_order(date(2025, 5, 31), TODAY - timedelta(days=5), 4000)
    d = decide_refund(o, TODAY)
    assert d.outcome is HUMAN and d.reason_codes == (R.POLICY_VERSION_NOT_IMPLEMENTED,)


# --- the ceiling: 7499 / 7500 / 7501 under v1, 11999 / 12000 / 12001 under v2 ----------------------


@pytest.mark.parametrize(
    ("purchase", "amount", "outcome"),
    [
        (V1_DAY, 7499, ALLOWED),
        (V1_DAY, 7500, ALLOWED),  # "above that" means strictly above
        (V1_DAY, 7501, HUMAN),
        (V1_DAY, 12000, HUMAN),  # the v2 ceiling is not the v1 ceiling
        (V2_DAY, 7501, ALLOWED),  # ... and 7501 is fine under v2
        (V2_DAY, 11999, ALLOWED),
        (V2_DAY, 12000, ALLOWED),
        (V2_DAY, 12001, HUMAN),
    ],
)
def test_the_auto_approve_ceiling_from_both_sides(
    purchase: date, amount: int, outcome: RefundOutcome
) -> None:
    d = decide_refund(delivered_order(purchase, TODAY - timedelta(days=3), amount), TODAY)
    assert d.outcome is outcome
    assert d.aggregate_cents == amount  # above the ceiling the amount is still computed
    assert d.authorized_cents == (amount if outcome is ALLOWED else 0)
    if outcome is HUMAN:
        assert d.reason_codes[0] is R.ABOVE_AUTO_APPROVE_CEILING and d.outcome is not DENIED


# --- the window: last valid day, the day it expires, the day after ---------------------------------


@pytest.mark.parametrize(
    ("purchase", "elapsed", "outcome"),
    [
        (V1_DAY, 29, ALLOWED),
        (V1_DAY, 30, ALLOWED),  # the last valid day (0 days left)
        (V1_DAY, 31, DENIED),  # the day the window has expired
        (V1_DAY, 32, DENIED),  # the day after
        (V2_DAY, 44, ALLOWED),
        (V2_DAY, 45, ALLOWED),
        (V2_DAY, 46, DENIED),
        (V2_DAY, 47, DENIED),
    ],
)
def test_the_refund_window_from_both_sides(
    purchase: date, elapsed: int, outcome: RefundOutcome
) -> None:
    o = delivered_order(purchase, TODAY - timedelta(days=elapsed), 4000)
    d = decide_refund(o, TODAY)
    assert d.outcome is outcome and d.facts["days_since_anchor"] == elapsed
    if outcome is DENIED:
        assert d.reason_codes == (R.REFUND_WINDOW_EXPIRED,) and d.aggregate_cents == 0
    else:
        assert d.aggregate_cents == 4000


def test_the_window_for_a_lost_shipment_runs_from_the_presumed_lost_date() -> None:
    shipped = date(2026, 3, 11)  # Wednesday; presumed lost 2026-03-25
    lost_on = date(2026, 3, 25)
    for elapsed, outcome in ((45, ALLOWED), (46, DENIED)):  # v2 window
        o = RefundOrder(
            "T-LOST", PurchaseDate(date(2026, 3, 10)), "shipped",
            (RefundItem("i", "S", "general", 4000),),
            RefundShipment("in_transit", shipped, None),
            paid_cents=4000,
        )  # fmt: skip
        assert decide_refund(o, lost_on + timedelta(days=elapsed)).outcome is outcome


# --- two dates, two jobs ---------------------------------------------------------------------------


def test_version_selection_and_window_measurement_are_driven_by_different_dates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fails if one date drives both. SIM-1005-shaped: purchase 02-25 (v1), delivered 03-05 (after the
    cutover), day 41. Version by DELIVERY date would pick v2 (45 days) and allow it; window from the
    PURCHASE date would make SIM-1001-shaped orders look expired. Both mistakes flip an outcome."""
    expired = delivered_order(date(2026, 2, 25), date(2026, 3, 5), 4000)
    d = decide_refund(expired, TODAY)
    assert d.policy_version == 1 and d.outcome is DENIED  # v1 by purchase; 41 > 30

    inside = delivered_order(date(2026, 2, 26), date(2026, 3, 18), 4000)  # day 28 since delivery
    assert (TODAY - inside.purchase_date).days == 48  # more than 45: from the purchase it'd expire
    d2 = decide_refund(inside, TODAY)
    assert d2.policy_version == 1 and d2.outcome is ALLOWED and d2.facts["days_since_anchor"] == 28

    # Structural: the purchase date is the ONLY argument to select_policy_version, and it is never
    # the window anchor, even when the two differ by a single day.
    seen: list[date] = []
    real = refund_module.select_policy_version

    def spy(p: PurchaseDate) -> refund_module.RefundTerms | None:
        seen.append(p)
        return real(p)

    monkeypatch.setattr(refund_module, "select_policy_version", spy)
    o = delivered_order(date(2026, 2, 28), date(2026, 3, 1), 4000)
    d3 = decide_refund(o, TODAY)
    assert seen == [date(2026, 2, 28)] and d3.policy_version == 1
    assert d3.facts["window_anchor"] == "2026-03-01" and d3.facts["purchase_date"] == "2026-02-28"


# --- discount first, then proration; the two paid-amount mechanisms ---------------------------------


def test_v2_cites_the_promo_clause_and_v1_relies_on_the_invariant_and_cites_nothing() -> None:
    v1 = decide_refund(
        delivered_order(V1_DAY, TODAY - timedelta(days=3), 3750, 2250, discount=2000), TODAY
    )
    v2 = decide_refund(
        delivered_order(V2_DAY, TODAY - timedelta(days=3), 3750, 2250, discount=2000), TODAY
    )
    assert v1.aggregate_cents == v2.aggregate_cents == 6000  # both refund what was paid
    assert (
        R.PAID_AMOUNT_BASIS_INVARIANT in v1.reason_codes
        and R.PAID_AMOUNT_BASIS_POLICY not in v1.reason_codes
    )
    assert (
        R.PAID_AMOUNT_BASIS_POLICY in v2.reason_codes
        and R.PAID_AMOUNT_BASIS_INVARIANT not in v2.reason_codes
    )
    assert "refund-v2-clause-4" in v2.decisive_clause_keys
    assert not any(
        k.endswith("clause-4") for k in v1.applied_clause_keys
    )  # nothing is cited for v1
    no_discount = decide_refund(delivered_order(V1_DAY, TODAY - timedelta(days=3), 6000), TODAY)
    assert R.PAID_AMOUNT_BASIS_INVARIANT not in no_discount.reason_codes


def test_proration_is_applied_to_the_amount_paid_after_the_discount_and_rounds_up() -> None:
    kw = {"items_total": 3, "items_shipped": 1}
    o = delivered_order(V1_DAY, TODAY - timedelta(days=3), 1001, discount=500, **kw)
    d = decide_refund(o, TODAY)
    assert lines(d) == [668]  # ceil(1001 * 2 / 3) = 668: paid (post-discount) first, then proration
    assert d.reason_codes[:2] == (R.REFUND_WITHIN_WINDOW, R.PRORATED_BY_ITEMS_SHIPPED)
    assert "refund-v1-clause-4" in d.decisive_clause_keys
    v2 = decide_refund(delivered_order(V2_DAY, TODAY - timedelta(days=3), 1001, **kw), TODAY)
    assert lines(v2) == [668] and R.RULE_CARRIED_FORWARD_FROM_V1 in v2.reason_codes
    # v2 cites its residual clause 6, "incorporating v1 clause 4"; it does not cite nothing.
    by_key = {c.key: c for c in v2.applied}
    assert by_key["refund-v2-clause-6"].decisive
    assert by_key["refund-v2-clause-6"].incorporating == ("refund-v1-clause-4",)


def test_a_fully_shipped_box_prorates_to_nothing_and_a_lost_one_is_not_prorated() -> None:
    kw = {"items_total": 4, "items_shipped": 4}
    d = decide_refund(delivered_order(V1_DAY, TODAY - timedelta(days=3), 3000, **kw), TODAY)
    assert d.outcome is DENIED
    assert R.NOTHING_REFUNDABLE in d.reason_codes and d.decisive_clause_keys == (
        "refund-v1-clause-4",
    )
    lost = RefundOrder(
        "T-LOST", PurchaseDate(V2_DAY), "shipped",
        (RefundItem("i", "S", "general", 3000, items_total=4, items_shipped=4),),
        RefundShipment("lost", date(2026, 4, 1), None), paid_cents=3000,
    )  # fmt: skip
    dl = decide_refund(lost, TODAY)  # never consumed, so not prorated (SPEC 18)
    assert dl.outcome is ALLOWED and lines(dl) == [3000]


# --- exclusions, prior refunds, and the system invariant --------------------------------------------


def test_final_sale_and_accessed_digital_lines_are_zero_whatever_the_window() -> None:
    o = RefundOrder(
        "T-2", PurchaseDate(V2_DAY), "delivered",
        (
            RefundItem("a", "A", "general", 3000, final_sale=True),
            RefundItem("b", "B", "digital_addon", 1500, digital_addon=True, digital_accessed_on=date(2026, 4, 2)),
            RefundItem("c", "C", "digital_addon", 1500, digital_addon=True),  # not accessed: refundable
        ),
        RefundShipment("delivered", date(2026, 4, 1), date(2026, 4, 5)), paid_cents=6000,
    )  # fmt: skip
    d = decide_refund(o, TODAY)
    assert lines(d) == [0, 0, 1500] and d.outcome is ALLOWED


def test_when_every_line_is_excluded_the_exclusions_are_the_decisive_clauses() -> None:
    o = delivered_order(V2_DAY, TODAY - timedelta(days=2), 3000, final_sale=True)
    d = decide_refund(o, TODAY)
    assert d.outcome is DENIED and R.NOTHING_REFUNDABLE in d.reason_codes
    assert d.decisive_clause_keys == ("refund-v2-clause-5",)
    assert "refund-v2-clause-1" in d.supporting_clause_keys


def test_already_refunded_amounts_are_not_refunded_again() -> None:
    o = RefundOrder(
        "T-3", PurchaseDate(V2_DAY), "delivered",
        (RefundItem("a", "A", "general", 3000, refunded_cents=1200), RefundItem("b", "B", "general", 2000, refunded_cents=2000)),
        RefundShipment("delivered", date(2026, 4, 1), date(2026, 4, 5)),
        paid_cents=5000, refunded_total_cents=3200,
    )  # fmt: skip
    d = decide_refund(o, TODAY)
    assert lines(d) == [1800, 0] and d.lines[1].reason_codes == (R.ALREADY_REFUNDED,)
    done = RefundOrder(
        **{
            **o.__dict__,
            "items": tuple(
                RefundItem(i.item_id, i.sku, i.category, i.paid_cents, i.paid_cents)
                for i in o.items
            ),
            "refunded_total_cents": 5000,
        }
    )
    assert decide_refund(done, TODAY).outcome is DENIED


def test_the_paid_amount_invariant_caps_the_order_even_when_the_lines_would_not() -> None:
    """The lines add up to more than the order was paid (an inconsistent record): the invariant, not
    a policy clause, cuts the aggregate to what the order was paid minus what was refunded."""
    o = RefundOrder(
        "T-4", PurchaseDate(V2_DAY), "delivered",
        (RefundItem("a", "A", "general", 3000), RefundItem("b", "B", "general", 3000)),
        RefundShipment("delivered", date(2026, 4, 1), date(2026, 4, 5)),
        paid_cents=5000, refunded_total_cents=1000,
    )  # fmt: skip
    d = decide_refund(o, TODAY)
    assert d.aggregate_cents == 4000 and lines(d) == [3000, 1000]
    assert R.PAID_AMOUNT_INVARIANT_CAP in d.reason_codes
    assert not any("invariant" in k for k in d.applied_clause_keys)  # it is not a policy clause


# --- safe by default ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("category", "restriction_clause"), [("hazardous", 3), ("perishable", 4), ("oversize", 6)]
)
def test_an_undelivered_order_with_a_conflicting_timeline_category_needs_a_human(
    category: str, restriction_clause: int
) -> None:
    o = RefundOrder(
        "T-5", PurchaseDate(V2_DAY), "shipped",
        (RefundItem("a", "A", category, 3000),),
        RefundShipment("in_transit", date(2026, 3, 11), None), paid_cents=3000,
    )  # fmt: skip
    d = decide_refund(o, TODAY)  # long past the 10-business-day general window: must NOT decide
    assert d.outcome is HUMAN and d.reason_codes == (R.CATEGORY_TIMELINE_NOT_IMPLEMENTED,)
    assert d.aggregate_cents == 0 and d.authorized_cents == 0
    assert set(d.decisive_clause_keys) == {
        "shipping-v1-clause-2", f"shipping_restrictions-v1-clause-{restriction_clause}"
    }  # fmt: skip
    assert d.handoff is not None and d.facts["categories"] == category


def test_a_delivered_hazardous_order_needs_no_timeline_and_is_decided_normally() -> None:
    o = delivered_order(V2_DAY, TODAY - timedelta(days=3), 3000)
    items = (RefundItem("a", "A", "hazardous", 3000),)
    d = decide_refund(RefundOrder(**{**o.__dict__, "items": items}), TODAY)
    assert d.outcome is ALLOWED


def test_a_canceled_order_and_inconsistent_records_are_not_guessed_at() -> None:
    o = delivered_order(V2_DAY, TODAY - timedelta(days=3), 3000)
    assert (
        decide_refund(RefundOrder(**{**o.__dict__, "status": "canceled"}), TODAY).outcome is DENIED
    )
    future = delivered_order(V2_DAY, TODAY + timedelta(days=2), 3000)
    d = decide_refund(future, TODAY)
    assert d.outcome is HUMAN and d.reason_codes == (R.RECORDS_INCONSISTENT,)


# --- unverifiable claims ------------------------------------------------------------------------------


def test_a_claim_the_outcome_depends_on_is_a_handoff_with_the_claim_verbatim() -> None:
    words = "I never got the box, the courier left it at the wrong house!"
    o = delivered_order(V2_DAY, TODAY - timedelta(days=5), 4000)  # records: delivered 5 days ago
    base = decide_refund(o, TODAY)
    d = decide_refund(o, TODAY, [Claim(ClaimKind.NOT_RECEIVED, words)])
    assert base.outcome is ALLOWED
    # If the claim were true the shipment is undelivered and not yet lost: a different decision.
    assert d.outcome is HUMAN and d.reason_codes == (R.UNVERIFIABLE_CLAIM,)
    assert d.aggregate_cents == 0 and d.authorized_cents == 0
    assert d.handoff is not None and d.handoff.evidence["claims_verbatim"] == [words]
    assert (
        d.handoff.evidence["records_say"] == "allowed"
        and d.handoff.evidence["if_claim_true"] == "denied"
    )
    assert d.decisive_clause_keys == () and set(d.supporting_clause_keys) == set(
        base.applied_clause_keys
    )


def test_an_accessed_addon_claim_depends_on_the_line_and_is_handed_off() -> None:
    o = from_seed("digital_addon_accessed_with_refundable_items")
    d = decide_refund(
        o, TODAY, [Claim(ClaimKind.DIGITAL_NOT_ACCESSED, "I never opened the recipes")]
    )
    assert d.outcome is HUMAN and d.reason_codes == (R.UNVERIFIABLE_CLAIM,)
    assert d.handoff is not None and d.handoff.evidence["if_claim_true_authorized_cents"] == 9500


def test_a_claim_that_changes_nothing_is_recorded_not_acted_on() -> None:
    expired = delivered_order(V1_DAY, TODAY - timedelta(days=50), 4000)  # denied either way
    claim = Claim(ClaimKind.NOT_RECEIVED, "never arrived")
    d = decide_refund(expired, TODAY, [claim])
    assert d.outcome is DENIED and d.facts["claims_recorded"] == 1 and d.handoff is None
    undelivered = from_seed("in_transit_on_schedule")  # records already say it has not arrived
    d2 = decide_refund(undelivered, TODAY, [claim])
    assert (
        d2.outcome is DENIED and d2.reason_codes == decide_refund(undelivered, TODAY).reason_codes
    )


def test_a_claim_never_turns_a_denial_into_an_allowance() -> None:
    claim = Claim(ClaimKind.NOT_RECEIVED, "it was never delivered, so the window has not started")
    expired = delivered_order(V1_DAY, TODAY - timedelta(days=50), 4000)
    d = decide_refund(expired, TODAY, [claim])
    assert d.outcome is DENIED and d.authorized_cents == 0


# --- the customer's message is not an input ------------------------------------------------------------


def test_decide_refund_takes_no_message() -> None:
    import inspect

    params = set(inspect.signature(decide_refund).parameters)
    assert params == {"order", "today", "claims"}
    assert not any("message" in p or "text" in p or "customer" in p for p in params)
