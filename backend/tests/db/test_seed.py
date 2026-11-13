"""Seeding against real PostgreSQL: idempotent, deterministic, invariant-preserving."""

import dataclasses
from typing import Any

import pytest
from sqlalchemy import Connection, text

from app.clock import SIMULATED_TODAY
from app.db.invariants import RefundDriftError, assert_no_refund_drift
from app.seed import definitions, ids, loader
from app.seed.definitions import CASES, CUSTOMERS, ORDERS, PRODUCTS, SUBSCRIPTIONS
from app.seed.loader import SeedChangedError, SeedConflictError, seed_all

# Every table the seed writes, with the columns that identify a row's CONTENT (not its
# identity-generated surrogate keys, which depend on sequence history across test runs).
_SNAPSHOT_SQL = {
    "customers": "SELECT * FROM customers ORDER BY id",
    "products": "SELECT * FROM products ORDER BY id",
    "subscriptions": "SELECT * FROM subscriptions ORDER BY id",
    "orders": "SELECT * FROM orders ORDER BY id",
    "order_items": "SELECT * FROM order_items ORDER BY id",
    "shipments": "SELECT * FROM shipments ORDER BY id",
    "shipment_events": (
        "SELECT shipment_id, customer_id, occurred_at, code, detail FROM shipment_events"
        " ORDER BY shipment_id, occurred_at, code"
    ),
}


def _snapshot(conn: Connection) -> dict[str, list[tuple[Any, ...]]]:
    return {t: [tuple(r) for r in conn.execute(text(sql))] for t, sql in _SNAPSHOT_SQL.items()}


def _seed_customer_ids() -> list[Any]:
    """Other tests commit their own rows into the shared test database (the concurrency tests),
    so assertions about 'the seed' are scoped to the seeded customers, never whole tables."""
    return [ids.customer_id(c.key) for c in CUSTOMERS]


@pytest.fixture
def seeded(conn: Connection) -> Connection:
    seed_all(conn)
    return conn


def test_first_run_inserts_everything_and_second_run_changes_nothing(conn: Connection) -> None:
    first = seed_all(conn)
    n_events = sum(len(o.shipment.events) for o in ORDERS if o.shipment)
    n_items = sum(len(o.items) + (1 if o.shipping_cents else 0) for o in ORDERS)  # + shipping lines
    n_ships = sum(1 for o in ORDERS if o.shipment)
    assert first.inserted == {
        "customers": len(CUSTOMERS),
        "products": len(PRODUCTS),
        "subscriptions": len(SUBSCRIPTIONS),
        "orders": len(ORDERS),
        "order_items": n_items,
        "shipments": n_ships,
        "shipment_events": n_events,
    }
    snap1 = _snapshot(conn)

    second = seed_all(conn)
    third = seed_all(conn)
    assert second.total_inserted == third.total_inserted == 0
    assert second.unchanged == third.unchanged == first.total_inserted
    assert _snapshot(conn) == snap1  # every column of every seeded row, row ids included


def test_ids_in_the_database_are_the_deterministic_ones_derived_from_keys(
    seeded: Connection,
) -> None:
    for c in CASES:
        table = "orders" if c.kind == "order" else "subscriptions"
        row_id = ids.order_id(c.key) if c.kind == "order" else ids.subscription_id(c.key)
        assert (
            seeded.execute(
                text(f"SELECT count(*) FROM {table} WHERE id = :i"),  # noqa: S608
                {"i": row_id},
            ).scalar_one()
            == 1
        ), c.key


def test_required_cases_in_the_database_have_the_properties_they_claim(
    seeded: Connection,
) -> None:
    """The unit tests check the definitions; this checks what actually landed in Postgres."""

    def q(sql: str, **p: Any) -> Any:
        return seeded.execute(text(sql), p).one()

    # 1: v1 purchase date, delivered after the cutover, 9800 paid
    r = q(
        "SELECT o.purchase_date, o.paid_cents, s.delivered_at::date AS d FROM orders o"
        " JOIN shipments s ON s.order_id = o.id WHERE o.id = :i",
        i=ids.order_id("refund_v1_above_ceiling_human_review"),
    )
    assert (str(r.purchase_date), r.paid_cents, str(r.d)) == ("2026-02-26", 9800, "2026-03-18")

    # 2: list differs from paid
    r = q(
        "SELECT list_total_cents, paid_cents, discount_cents FROM orders WHERE id = :i",
        i=ids.order_id("promo_discount_paid_differs_from_list"),
    )
    assert (r.list_total_cents, r.discount_cents, r.paid_cents) == (8000, 2000, 6500)

    # 3: an accessed digital add-on beside non-digital items
    r = q(
        "SELECT count(*) FILTER (WHERE p.is_digital_addon AND i.digital_accessed_at IS NOT NULL)"
        " AS accessed, count(*) FILTER (WHERE NOT p.is_digital_addon) AS physical"
        " FROM order_items i JOIN products p ON p.id = i.product_id WHERE i.order_id = :i",
        i=ids.order_id("digital_addon_accessed_with_refundable_items"),
    )
    assert (r.accessed, r.physical) == (1, 2)

    # 4: a final_sale line beside a refundable one
    r = q(
        "SELECT count(*) FILTER (WHERE final_sale) AS fs, count(*) FILTER (WHERE NOT final_sale)"
        " AS ok FROM order_items WHERE order_id = :i AND line_type = 'item'",
        i=ids.order_id("final_sale_item_in_order"),
    )
    assert (r.fs, r.ok) == (1, 1)

    # 5: undelivered original, and a replacement that points at it
    r = q(
        "SELECT s.delivered_at, rep.order_number, rep.paid_cents FROM shipments s"
        " JOIN orders rep ON rep.replacement_for_order_id = s.order_id WHERE s.order_id = :i",
        i=ids.order_id("lost_shipment_with_replacement_sent"),
    )
    assert (r.delivered_at, r.order_number, r.paid_cents) == (None, "SIM-2002", 0)

    # 6, 7, 8: subscriptions with the plan type and first-charge day count we claim
    rows = {
        r.id: r
        for r in seeded.execute(
            text(
                "SELECT id, plan_type, (CAST(:today AS date) - (first_charge_at AT TIME ZONE 'UTC')::date)"
                " AS day FROM subscriptions"
            ),
            {"today": SIMULATED_TODAY},
        )
    }
    s6 = rows[ids.subscription_id("annual_v1_inside_proration_window")]
    s7 = rows[ids.subscription_id("monthly_v2_inside_cooling_off")]
    s8 = rows[ids.subscription_id("annual_v2_day10_cooling_off_beats_proration")]
    assert (s6.plan_type, s6.day) == ("annual", 54)
    assert (s7.plan_type, s7.day) == ("monthly", 7)
    assert (s8.plan_type, s8.day) == ("annual", 10)


def test_cross_customer_ownership_holds_for_every_seeded_row(seeded: Connection) -> None:
    """Customers must have disjoint data, and no child names a different customer's parent."""
    per_customer = seeded.execute(
        text(
            "SELECT customer_id, count(*) AS n FROM orders WHERE customer_id = ANY(:c)"
            " GROUP BY customer_id"
        ),
        {"c": _seed_customer_ids()},
    ).all()
    assert len(per_customer) == len(CUSTOMERS) and all(r.n > 0 for r in per_customer)
    bad = seeded.execute(
        text(
            "SELECT 1 FROM order_items i JOIN orders o ON o.id = i.order_id"
            " WHERE o.customer_id <> i.customer_id"
            " UNION ALL SELECT 1 FROM shipments s JOIN orders o ON o.id = s.order_id"
            " WHERE o.customer_id <> s.customer_id"
            " UNION ALL SELECT 1 FROM orders r JOIN orders o ON o.id = r.replacement_for_order_id"
            " WHERE o.customer_id <> r.customer_id"
        )
    ).all()  # composite FKs make these impossible anywhere, so the whole-table scope is fine
    assert bad == []


def test_no_refunds_are_seeded_and_the_drift_assertion_passes(seeded: Connection) -> None:
    c = {"c": _seed_customer_ids()}
    refunds = text("SELECT count(*) FROM refunds WHERE customer_id = ANY(:c)")
    refunded = text(
        "SELECT coalesce(sum(refunded_total_cents), 0) FROM orders WHERE customer_id = ANY(:c)"
    )
    assert seeded.execute(refunds, c).scalar_one() == 0
    assert seeded.execute(refunded, c).scalar_one() == 0
    assert_no_refund_drift(seeded)


def test_seeding_fails_if_the_invariant_check_trips(
    conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_: Connection) -> None:
        raise RefundDriftError("simulated drift")

    monkeypatch.setattr(loader, "assert_no_refund_drift", boom)
    with pytest.raises(RefundDriftError):
        seed_all(conn)


def test_seed_text_is_labeled_simulated_in_the_database(seeded: Connection) -> None:
    unlabeled = seeded.execute(
        text(
            "SELECT display_name FROM customers WHERE id = ANY(:c)"
            "   AND display_name NOT LIKE '%SIMULATED%'"
            " UNION ALL SELECT name FROM products WHERE sku LIKE 'SIM-%'"
            "   AND name NOT LIKE '%SIMULATED%'"
            " UNION ALL SELECT carrier FROM shipments WHERE customer_id = ANY(:c)"
            "   AND carrier NOT LIKE '%SIMULATED%'"
            " UNION ALL SELECT detail FROM shipment_events WHERE customer_id = ANY(:c)"
            "   AND detail NOT LIKE '%SIMULATED%'"
        ),
        {"c": _seed_customer_ids()},
    ).all()
    assert unlabeled == []


# --- re-seeding must notice drift instead of papering over it -----------------------------


def test_a_changed_definition_is_detected_not_silently_ignored(
    seeded: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    edited = tuple(
        dataclasses.replace(p, price_cents=p.price_cents + 1) if p.sku == "SIM-BOX-MINI" else p
        for p in definitions.PRODUCTS
    )
    monkeypatch.setattr(loader, "PRODUCTS", edited)
    with pytest.raises(SeedChangedError, match="SIM-BOX-MINI|price_cents"):
        seed_all(seeded)


def test_a_changed_shipment_event_is_detected(
    seeded: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def tweak(o: definitions.OrderDef) -> definitions.OrderDef:
        if o.key != "lost_shipment_with_replacement_sent" or o.shipment is None:
            return o
        events = o.shipment.events[:-1]  # drop the last hub scan
        return dataclasses.replace(o, shipment=dataclasses.replace(o.shipment, events=events))

    monkeypatch.setattr(loader, "ORDERS", tuple(tweak(o) for o in definitions.ORDERS))
    with pytest.raises(SeedChangedError, match="shipment_events"):
        seed_all(seeded)


def test_a_colliding_unrelated_row_is_reported(conn: Connection) -> None:
    conn.execute(
        text(
            "INSERT INTO customers (email, display_name, password_hash)"
            " VALUES ('alex.rivera@simulated.example', 'Someone else', 'x')"
        )
    )
    with pytest.raises(SeedConflictError):
        seed_all(conn)


def test_live_state_changes_do_not_make_reseeding_fail(seeded: Connection) -> None:
    """Statuses are state, not definition: after the system acts, re-seeding is still a no-op."""
    seeded.execute(
        text(
            "UPDATE subscriptions SET status = 'canceled', canceled_at = clock_timestamp(),"
            " cancel_effective_at = clock_timestamp() WHERE id = :i"
        ),
        {"i": ids.subscription_id("annual_v2_day10_cooling_off_beats_proration")},
    )
    assert seed_all(seeded).total_inserted == 0
