"""Property-based tests of the refund rules: invariants that must hold for ANY input, not just the
seeded ones. Hypothesis generates orders, shipments, dates and claims (including inconsistent money
records, which is where the paid-amount invariant earns its keep). No database, no network.

derandomize=True: the same examples on every run, so CI cannot flake, and a failure reproduces.
"""

from datetime import date, timedelta

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.domain.rules.refund import (
    TERMS,
    Claim,
    ClaimKind,
    RefundItem,
    RefundOrder,
    RefundOutcome,
    RefundReplacement,
    RefundShipment,
    decide_refund,
    select_policy_version,
)
from app.domain.types import PurchaseDate
from app.retrieval.policy_loader import load_policies

CORPUS_KEYS = {d.chunk_key(c.number) for d in load_policies() for c in d.clauses}
SETTINGS = settings(
    max_examples=400,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow],
)
BASE = date(2025, 5, 1)  # starts before the first policy so "no version in effect" is generated
days = st.integers(0, 600).map(lambda n: BASE + timedelta(days=n))
cents = st.integers(0, 30000)
CATEGORIES = ["general", "plan", "hazardous", "perishable", "oversize", "age_restricted"]


@st.composite
def items(draw: st.DrawFn) -> RefundItem:
    paid = draw(cents)
    refunded = draw(st.integers(0, paid))
    boxed = draw(st.booleans())
    total = draw(st.integers(1, 12)) if boxed else None
    digital = draw(st.booleans())
    return RefundItem(
        item_id=f"i{draw(st.integers(0, 10**6))}",
        sku="S",
        category=draw(st.sampled_from(CATEGORIES)),
        paid_cents=paid,
        refunded_cents=refunded,
        final_sale=draw(st.booleans()),
        digital_addon=digital,
        digital_accessed_on=draw(st.none() | days) if digital else None,
        items_total=total,
        items_shipped=None if total is None else draw(st.integers(0, total)),
    )


@st.composite
def shipping_lines(draw: st.DrawFn) -> RefundItem:
    paid = draw(st.integers(1, 2000))
    return RefundItem(
        "ship", "SHIPPING", "shipping", paid, draw(st.integers(0, paid)), line_type="shipping"
    )


@st.composite
def replacements(draw: st.DrawFn) -> RefundReplacement:
    return RefundReplacement(
        "R-1",
        draw(st.sampled_from(["placed", "shipped", "delivered", "canceled"])),
        draw(shipments()),
    )


@st.composite
def shipments(draw: st.DrawFn) -> RefundShipment | None:
    if draw(st.integers(0, 9)) == 0:
        return None
    shipped = draw(st.none() | days)
    delivered = draw(st.none() | days)
    return RefundShipment(
        status=draw(
            st.sampled_from(["label_created", "in_transit", "delivered", "exception", "lost"])
        ),
        shipped_on=shipped,
        delivered_on=delivered,
        has_delivery_exception=draw(st.booleans()),
    )


@st.composite
def orders(draw: st.DrawFn) -> RefundOrder:
    its = tuple(draw(st.lists(items(), min_size=0, max_size=5)))
    if draw(st.booleans()):  # shipping is an order line (migration 0014)
        its += (draw(shipping_lines()),)
    # The order's own paid total is NOT tied to the sum of its lines: a mismatched record is exactly
    # what the invariant must survive.
    paid = draw(cents)
    return RefundOrder(
        order_number="P-1",
        purchase_date=PurchaseDate(draw(days)),
        status=draw(st.sampled_from(["placed", "shipped", "delivered", "canceled"])),
        items=its,
        shipment=draw(shipments()),
        kind=draw(st.sampled_from(["purchase", "purchase", "purchase", "subscription_charge"])),
        list_total_cents=paid + draw(cents),
        discount_cents=draw(st.integers(0, 3000)),
        shipping_cents=draw(st.integers(0, 2000)),
        paid_cents=paid,
        refunded_total_cents=draw(st.integers(0, paid)),
        replacements=tuple(draw(st.lists(replacements(), max_size=2))),
    )


TODAYS = st.integers(150, 700).map(lambda n: BASE + timedelta(days=n))
claims = st.lists(
    st.builds(Claim, st.sampled_from(list(ClaimKind)), st.text(max_size=60)), max_size=2
)


@given(orders(), TODAYS)
@SETTINGS
def test_a_refund_never_exceeds_what_was_paid_per_line_or_in_aggregate(
    order: RefundOrder, today: date
) -> None:
    d = decide_refund(order, today)
    assert len(d.lines) == len(order.items)
    for item, line in zip(order.items, d.lines, strict=True):
        assert 0 <= line.amount_cents <= item.paid_cents - item.refunded_cents
    assert d.aggregate_cents <= order.paid_cents - order.refunded_total_cents
    assert d.authorized_cents <= order.paid_cents - order.refunded_total_cents


@given(orders(), TODAYS)
@SETTINGS
def test_final_sale_and_accessed_digital_lines_never_contribute(
    order: RefundOrder, today: date
) -> None:
    d = decide_refund(order, today)
    for item, line in zip(order.items, d.lines, strict=True):
        if item.final_sale:
            assert line.amount_cents == 0
        if item.digital_addon and item.digital_accessed_on is not None:
            assert line.amount_cents == 0


@given(orders(), TODAYS)
@SETTINGS
def test_the_aggregate_is_the_sum_of_the_lines_and_only_an_allowed_decision_authorizes(
    order: RefundOrder, today: date
) -> None:
    d = decide_refund(order, today)
    assert d.aggregate_cents == sum(line.amount_cents for line in d.lines)
    if d.outcome is RefundOutcome.ALLOWED:
        terms = select_policy_version(order.purchase_date)
        assert terms is not None and d.authorized_cents == d.aggregate_cents
        assert 0 < d.aggregate_cents
        # The ceiling is CUMULATIVE per order: prior refunds count (SPEC 18.6).
        assert order.refunded_total_cents + d.aggregate_cents <= terms.ceiling_cents
    else:
        assert d.authorized_cents == 0


@given(orders(), TODAYS)
@SETTINGS
def test_citations_are_real_clauses_decisive_is_a_subset_and_the_version_follows_the_purchase(
    order: RefundOrder, today: date
) -> None:
    d = decide_refund(order, today)
    keys = d.applied_clause_keys
    assert len(keys) == len(set(keys)) and set(keys) <= CORPUS_KEYS
    assert set(d.decisive_clause_keys) <= set(keys)
    assert set(d.decisive_clause_keys).isdisjoint(d.supporting_clause_keys)
    terms = select_policy_version(order.purchase_date)
    assert d.policy_version == (0 if terms is None else terms.version)
    for k in keys:
        if k.startswith("refund-v"):
            assert k.startswith(f"refund-v{d.policy_version}-")  # never a clause of another version
    for c in d.applied:  # a residual clause names the earlier-version clauses it carries forward
        assert set(c.incorporating) <= CORPUS_KEYS
        assert not c.incorporating or c.key.startswith("refund-v2-")
    if d.outcome is RefundOutcome.ALLOWED:
        assert d.decisive_clause_keys  # an allowance always rests on a cited clause
    if d.handoff is not None:
        assert d.outcome is RefundOutcome.NEEDS_HUMAN


@given(orders(), TODAYS, claims)
@SETTINGS
def test_a_claim_can_only_escalate_never_grant_or_raise_an_amount(
    order: RefundOrder, today: date, cs: list[Claim]
) -> None:
    plain = decide_refund(order, today)
    with_claims = decide_refund(order, today, cs)
    assert with_claims.authorized_cents <= plain.authorized_cents
    if plain.outcome != RefundOutcome.ALLOWED:  # a claim cannot grant what the records withhold
        assert with_claims.outcome in (plain.outcome, RefundOutcome.NEEDS_HUMAN)
    if with_claims.outcome != plain.outcome:  # the only way a claim changes anything: a handoff
        assert with_claims.outcome is RefundOutcome.NEEDS_HUMAN
        assert with_claims.handoff is not None
        assert with_claims.handoff.evidence["claims_verbatim"] == [c.verbatim for c in cs]
        assert with_claims.decisive_clause_keys == ()


@given(orders(), TODAYS)
@SETTINGS
def test_repeated_refunds_cannot_cumulatively_exceed_the_amount_paid(
    order: RefundOrder, today: date
) -> None:
    """Apply each authorized decision as a refund and ask again: the running total stays within what
    was paid (the invariant holds across requests, not only within one)."""
    cur = order
    for _ in range(3):
        d = decide_refund(cur, today)
        if d.authorized_cents == 0:
            break
        new_items = tuple(
            RefundItem(
                i.item_id,
                i.sku,
                i.category,
                i.paid_cents,
                i.refunded_cents + line.amount_cents,
                i.final_sale,
                i.digital_addon,
                i.digital_accessed_on,
                i.items_total,
                i.items_shipped,
                i.line_type,
            )  # fmt: skip
            for i, line in zip(cur.items, d.lines, strict=True)
        )
        cur = RefundOrder(
            **{
                **cur.__dict__,
                "items": new_items,
                "refunded_total_cents": cur.refunded_total_cents + d.authorized_cents,
            }
        )
        assert cur.refunded_total_cents <= cur.paid_cents
        assert all(i.refunded_cents <= i.paid_cents for i in cur.items)


@given(orders(), TODAYS)
@SETTINGS
def test_the_engine_is_deterministic(order: RefundOrder, today: date) -> None:
    assert decide_refund(order, today) == decide_refund(order, today)


def test_the_generator_reaches_every_outcome_and_both_versions() -> None:
    """A property suite that never reaches a branch proves nothing about it: check the generator."""
    from hypothesis import find

    for outcome in RefundOutcome:
        find(
            st.tuples(orders(), TODAYS),
            lambda t, o=outcome: decide_refund(*t).outcome is o,  # type: ignore[misc]
            settings=settings(max_examples=2000, derandomize=True, deadline=None),
        )
    for version in (t.version for t in TERMS):
        find(
            st.tuples(orders(), TODAYS),
            lambda t, v=version: (
                decide_refund(*t).outcome is RefundOutcome.ALLOWED  # type: ignore[misc]
                and decide_refund(*t).policy_version == v
            ),
            settings=settings(max_examples=3000, derandomize=True, deadline=None),
        )


@given(orders(), TODAYS)
@SETTINGS
def test_a_replacement_that_is_not_itself_lost_blocks_a_refund_of_the_original_shipment(
    order: RefundOrder, today: date
) -> None:
    """Shipping v1 clause 4: while any replacement is the remedy, an undelivered original is never
    allowed or escalated into an allowance; it is denied with REPLACEMENT_IS_REMEDY."""
    d = decide_refund(order, today)
    if d.outcome is RefundOutcome.ALLOWED and order.shipment is not None:
        if order.shipment.delivered_on is None:  # the lost / exception paths
            from app.domain.rules.refund import _replacement_presumed_lost

            assert all(_replacement_presumed_lost(r, today) for r in order.replacements)


@given(orders(), TODAYS)
@SETTINGS
def test_shipping_is_refunded_only_when_the_whole_shipment_is_undelivered(
    order: RefundOrder, today: date
) -> None:
    d = decide_refund(order, today)
    for item, line in zip(order.items, d.lines, strict=True):
        if item.line_type == "shipping" and line.amount_cents > 0:
            assert order.shipment is not None and order.shipment.delivered_on is None
