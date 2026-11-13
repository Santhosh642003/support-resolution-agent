"""Migration 0014: shipping as an order line, and item counts on box lines (SPEC 18.6).

The database guarantees the SHAPE (one shipping line per order, no product on it, header equals
line, box counts coherent); whether a shipping line is refundable is a rule in the engine.
"""

import uuid
from uuid import UUID

from sqlalchemy import Connection, create_engine, text

from alembic import command
from app.db.bootstrap import bootstrap
from app.db.invariants import assert_no_refund_drift
from tests.db.conftest import (
    APP_PASSWORD,
    OWNER_PASSWORD,
    _alembic_config,
    _role_url,
    admin_base_url,
)
from tests.db.helpers import (
    expect_fail,
    item_ids,
    make_action,
    make_customer,
    make_decision,
    make_order,
    make_refund,
    one,
)


def shipping_line_id(conn: Connection, order: UUID) -> UUID:
    return one(  # type: ignore[no-any-return]
        conn, "SELECT id FROM order_items WHERE order_id = :o AND line_type = 'shipping'", o=order
    )


def test_an_order_with_shipping_has_exactly_one_shipping_line_equal_to_the_header(
    conn: Connection,
) -> None:
    c = make_customer(conn)
    o = make_order(conn, c, list_total=10000, shipping=500)
    conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))  # the deferred header = line check passes
    row = conn.execute(
        text(
            "SELECT product_id, paid_cents, unit_price_cents, qty FROM order_items"
            " WHERE order_id = :o AND line_type = 'shipping'"
        ),
        {"o": o},
    ).one()
    assert row.product_id is None and row.paid_cents == row.unit_price_cents == 500 and row.qty == 1
    assert len(item_ids(conn, o)) == 1  # the item lines are unchanged


def test_a_second_shipping_line_on_one_order_is_refused(conn: Connection) -> None:
    c = make_customer(conn)
    o = make_order(conn, c, shipping=500)
    state, constraint = expect_fail(
        conn,
        "INSERT INTO order_items (order_id, customer_id, line_type, qty, unit_price_cents,"
        " paid_cents) VALUES (:o, :c, 'shipping', 1, 0, 0)",
        o=o,
        c=c,
    )
    assert (state, constraint) == ("23505", "order_items_one_shipping_line_uq")


def test_a_shipping_line_has_no_product_and_an_item_line_always_has_one(conn: Connection) -> None:
    c = make_customer(conn)
    o = make_order(conn, c)
    product = one(conn, "SELECT product_id FROM order_items WHERE order_id = :o", o=o)
    state, constraint = expect_fail(
        conn,
        "INSERT INTO order_items (order_id, customer_id, product_id, line_type, qty,"
        " unit_price_cents, paid_cents) VALUES (:o, :c, :p, 'shipping', 1, 5, 5)",
        o=o,
        c=c,
        p=product,
    )
    assert (state, constraint) == ("23514", "order_items_product_by_type")
    state, constraint = expect_fail(
        conn,
        "INSERT INTO order_items (order_id, customer_id, line_type, qty, unit_price_cents,"
        " paid_cents) VALUES (:o, :c, 'item', 1, 5, 5)",
        o=o,
        c=c,
    )
    assert (state, constraint) == ("23514", "order_items_product_by_type")


def test_a_shipping_line_is_never_final_sale_or_a_box(conn: Connection) -> None:
    c = make_customer(conn)
    o = make_order(conn, c, shipping=500)
    sid = shipping_line_id(conn, o)
    for assignment in ("final_sale = true", "items_total = 2, items_shipped = 1"):
        state, constraint = expect_fail(
            conn,
            f"UPDATE order_items SET {assignment} WHERE id = :i",  # noqa: S608
            i=sid,
        )
        assert state == "23514"
        assert constraint in ("order_items_shipping_plain",)


def test_the_header_and_the_shipping_line_must_agree_at_commit(conn: Connection) -> None:
    c = make_customer(conn)
    o = make_order(conn, c, list_total=10000, shipping=500)
    conn.execute(
        text("UPDATE order_items SET paid_cents = 400 WHERE id = :i"),
        {"i": shipping_line_id(conn, o)},
    )
    state, _ = expect_fail(conn, "SET CONSTRAINTS ALL IMMEDIATE")
    assert state == "23514"


def test_shipping_in_the_header_without_a_line_is_refused_at_commit(conn: Connection) -> None:
    c = make_customer(conn)
    one(
        conn,
        "INSERT INTO orders (order_number, customer_id, placed_at, list_total_cents,"
        " discount_cents, shipping_cents, paid_cents)"
        " VALUES (:n, :c, now(), 1000, 0, 300, 1300) RETURNING id",
        n=f"SIM-{uuid.uuid4().hex[:8]}",
        c=c,
    )
    state, _ = expect_fail(conn, "SET CONSTRAINTS ALL IMMEDIATE")
    assert state == "23514"


def test_box_item_counts_are_both_or_neither_and_coherent(conn: Connection) -> None:
    c = make_customer(conn)
    o = make_order(conn, c)
    (item,) = item_ids(conn, o)
    for assignment in (
        "items_total = 3",  # one without the other
        "items_total = 3, items_shipped = 4",  # shipped above total
        "items_total = 0, items_shipped = 0",  # an empty box
        "items_total = 3, items_shipped = -1",
    ):
        state, constraint = expect_fail(
            conn,
            f"UPDATE order_items SET {assignment} WHERE id = :i",  # noqa: S608
            i=item,
        )
        assert (state, constraint) == ("23514", "order_items_box_counts"), assignment
    conn.execute(
        text("UPDATE order_items SET items_total = 3, items_shipped = 1 WHERE id = :i"), {"i": item}
    )


def test_a_shipping_line_is_refunded_by_the_same_machinery_and_the_guards_bind_it(
    conn: Connection,
) -> None:
    """The reason it is a line: refund_lines, the per-line cap, the order-level invariant and the
    drift views all apply to it unchanged."""
    c = make_customer(conn)
    o = make_order(conn, c, list_total=10000, shipping=500)
    (item,) = item_ids(conn, o)
    ship = shipping_line_id(conn, o)
    make_refund(conn, c, o, make_action(conn, c, make_decision(conn, c, o, amount=10500), amount=10500),
                10500, lines=[(item, 10000), (ship, 500)])  # fmt: skip
    assert one(conn, "SELECT refunded_cents FROM order_items WHERE id = :i", i=ship) == 500
    assert one(conn, "SELECT refunded_total_cents FROM orders WHERE id = :o", o=o) == 10500
    assert_no_refund_drift(conn)
    state, constraint = expect_fail(
        conn,
        "WITH r AS (INSERT INTO refunds (action_id, order_id, customer_id, amount_cents)"
        " VALUES (:a, :o, :c, 1) RETURNING id)"
        " INSERT INTO refund_lines (refund_id, order_item_id, order_id, customer_id, amount_cents)"
        " SELECT id, :i, :o, :c, 1 FROM r",
        a=make_action(conn, c, make_decision(conn, c, o, amount=1), amount=1),
        o=o,
        c=c,
        i=ship,
    )
    assert state == "23514"  # the order total or the line cap, whichever fires first
    assert constraint in ("order_items_refunded_within_paid", "orders_refund_within_paid")


def test_the_seeded_orders_have_their_shipping_lines_and_the_box_counts(conn: Connection) -> None:
    from app.seed import ids
    from app.seed.definitions import ORDERS
    from app.seed.loader import seed_all

    seed_all(conn)
    conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    for o in ORDERS:
        lines = conn.execute(
            text(
                "SELECT paid_cents FROM order_items WHERE order_id = :o AND line_type = 'shipping'"
            ),
            {"o": ids.order_id(o.key)},
        ).all()
        assert [r.paid_cents for r in lines] == ([o.shipping_cents] if o.shipping_cents else []), (
            o.key
        )
    box = conn.execute(
        text("SELECT items_total, items_shipped, paid_cents FROM order_items WHERE id = :i"),
        {"i": ids.item_id("partially_shipped_subscription_box", 0)},
    ).one()
    assert (box.items_total, box.items_shipped, box.paid_cents) == (3, 1, 5000)
    again = seed_all(conn)  # idempotent with the shipping lines present
    assert again.total_inserted == 0


# --- the backfill: a database that already holds orders with shipping ------------------------------


def test_upgrading_a_database_that_already_has_orders_backfills_the_shipping_lines() -> None:
    """Migrate to 0013, insert orders with and without shipping, upgrade to 0014 as the OWNER (FORCE
    row-level security applies to it): every shipping order gets exactly its line, and FORCE is back
    on afterwards."""
    base = admin_base_url()
    name = f"support_test_{uuid.uuid4().hex[:10]}"
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}" STRATEGY = WAL_LOG'))
    try:
        superuser = create_engine(base.set(database=name))
        with superuser.begin() as c:
            c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            c.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
        bootstrap(superuser, OWNER_PASSWORD, APP_PASSWORD)
        cfg = _alembic_config(_role_url(base, name, "support_owner", OWNER_PASSWORD))
        command.upgrade(cfg, "0013")
        with superuser.begin() as c:
            cust = [make_customer(c), make_customer(c)]
        # At 0013 there are no shipping lines: insert the old shape by hand.
        with superuser.begin() as c:
            for who, shipping in ((cust[0], 500), (cust[1], 0), (cust[1], 750)):
                order = one(
                    c,
                    "INSERT INTO orders (order_number, customer_id, placed_at, list_total_cents,"
                    " discount_cents, shipping_cents, paid_cents)"
                    " VALUES (:n, :c, now(), 5000, 0, :s, :p) RETURNING id",
                    n=f"SIM-{uuid.uuid4().hex[:8]}",
                    c=who,
                    s=shipping,
                    p=5000 + shipping,
                )
                product = one(
                    c,
                    "INSERT INTO products (sku, name, price_cents) VALUES (:s, 'x', 100) RETURNING id",
                    s=uuid.uuid4().hex,
                )
                c.execute(
                    text(
                        "INSERT INTO order_items (order_id, customer_id, product_id, qty,"
                        " unit_price_cents, paid_cents) VALUES (:o, :c, :p, 1, 5000, 5000)"
                    ),
                    {"o": order, "c": who, "p": product},
                )
        command.upgrade(cfg, "head")
        with superuser.connect() as c:
            lines = sorted(
                r.paid_cents
                for r in c.execute(
                    text("SELECT paid_cents FROM order_items WHERE line_type = 'shipping'")
                )
            )
            assert lines == [500, 750]
            forced: dict[str, bool] = dict(
                c.execute(
                    text(
                        "SELECT relname, relforcerowsecurity FROM pg_class"
                        " WHERE relname IN ('orders', 'order_items')"
                    )
                ).all()
            )
            assert forced == {"orders": True, "order_items": True}
        superuser.dispose()
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
