"""Line-level refunds: per-item cumulative guard, header/line agreement, two-level drift."""

from uuid import UUID

from sqlalchemy import Connection, text

from app.db.invariants import (
    RefundDriftError,
    assert_no_refund_drift,
    find_refund_line_drift,
)
from tests.db.helpers import (
    expect_fail,
    item_ids,
    make_action,
    make_customer,
    make_decision,
    make_item,
    make_order,
    make_refund,
    one,
)


def _action(conn: Connection, c: UUID, o: UUID, amount: int) -> UUID:
    return make_action(conn, c, make_decision(conn, c, o, amount=amount), amount=amount)


def test_refund_spans_items_and_updates_each_running_total(conn: Connection) -> None:
    c = make_customer(conn)
    o = make_order(conn, c, list_total=10000, items=[6000, 4000])
    big, small = item_ids(conn, o)
    make_refund(
        conn,
        c,
        o,
        _action(conn, c, o, 7000),
        7000,
        lines=[(big, 4000), (small, 3000)],
    )
    assert one(conn, "SELECT refunded_cents FROM order_items WHERE id=:i", i=big) == 4000
    assert one(conn, "SELECT refunded_cents FROM order_items WHERE id=:i", i=small) == 3000
    assert one(conn, "SELECT refunded_total_cents FROM orders WHERE id=:o", o=o) == 7000
    assert_no_refund_drift(conn)


def test_cumulative_per_item_guard_fires_even_when_order_level_has_room(conn: Connection) -> None:
    c = make_customer(conn)
    # Item paid 10000 plus 5000 shipping: the order total (15000) has room that the item lacks.
    o = make_order(conn, c, list_total=10000, shipping=5000)
    (item,) = item_ids(conn, o)
    make_refund(conn, c, o, _action(conn, c, o, 6000), 6000, lines=[(item, 6000)])
    a2 = _action(conn, c, o, 5000)
    state, constraint = expect_fail(
        conn,
        "WITH r AS (INSERT INTO refunds (action_id, order_id, customer_id, amount_cents)"
        " VALUES (:a, :o, :c, 5000) RETURNING id)"
        " INSERT INTO refund_lines (refund_id, order_item_id, order_id, customer_id, amount_cents)"
        " SELECT id, :i, :o, :c, 5000 FROM r",
        a=a2,
        o=o,
        c=c,
        i=item,
    )
    assert (state, constraint) == ("23514", "order_items_refunded_within_paid")


def test_one_line_per_item_per_refund(conn: Connection) -> None:
    c = make_customer(conn)
    o = make_order(conn, c, items=[6000, 4000])
    big, _ = item_ids(conn, o)
    refund = make_refund(conn, c, o, _action(conn, c, o, 3000), 3000, lines=[(big, 3000)])
    state, constraint = expect_fail(
        conn,
        "INSERT INTO refund_lines (refund_id, order_item_id, order_id, customer_id, amount_cents)"
        " VALUES (:r, :i, :o, :c, 100)",
        r=refund,
        i=big,
        o=o,
        c=c,
    )
    assert (state, constraint) == ("23505", "refund_lines_refund_item_uq")


def test_line_cannot_reference_an_item_from_another_order_or_customer(conn: Connection) -> None:
    c, other = make_customer(conn), make_customer(conn)
    o = make_order(conn, c)
    foreign_order = make_order(conn, other)
    (foreign_item,) = item_ids(conn, foreign_order)
    own_item = make_item(conn, c, make_order(conn, c, items=[]), 100)
    refund = make_refund(conn, c, o, _action(conn, c, o, 1000), 1000)
    state, constraint = expect_fail(
        conn,
        "INSERT INTO refund_lines (refund_id, order_item_id, order_id, customer_id, amount_cents)"
        " VALUES (:r, :i, :o, :c, 100)",
        r=refund,
        i=foreign_item,
        o=o,
        c=c,
    )
    assert (state, constraint) == ("23503", "refund_lines_item_order_fk")
    # Same customer but a different order's item is also rejected.
    state, constraint = expect_fail(
        conn,
        "INSERT INTO refund_lines (refund_id, order_item_id, order_id, customer_id, amount_cents)"
        " VALUES (:r, :i, :o, :c, 100)",
        r=refund,
        i=own_item,
        o=o,
        c=c,
    )
    assert (state, constraint) == ("23503", "refund_lines_item_order_fk")


def test_header_must_equal_sum_of_lines_at_commit(conn: Connection) -> None:
    c = make_customer(conn)
    o = make_order(conn, c)
    a = _action(conn, c, o, 3000)
    # A header with no lines, and one whose lines do not add up, both fail when constraints fire.
    conn.execute(
        text(
            "INSERT INTO refunds (action_id, order_id, customer_id, amount_cents)"
            " VALUES (:a, :o, :c, 3000)"
        ),
        {"a": a, "o": o, "c": c},
    )
    state, _ = expect_fail(conn, "SET CONSTRAINTS ALL IMMEDIATE")
    assert state == "23514"


def test_lines_not_matching_header_fail(conn: Connection) -> None:
    c = make_customer(conn)
    o = make_order(conn, c)
    (item,) = item_ids(conn, o)
    make_refund(conn, c, o, _action(conn, c, o, 3000), 3000, lines=[(item, 2000)])
    state, _ = expect_fail(conn, "SET CONSTRAINTS ALL IMMEDIATE")
    assert state == "23514"


def test_refund_lines_are_immutable(conn: Connection) -> None:
    c = make_customer(conn)
    o = make_order(conn, c)
    make_refund(conn, c, o, _action(conn, c, o, 1000), 1000)
    for sql in (
        "UPDATE refund_lines SET amount_cents = 1",
        "DELETE FROM refund_lines",
        "TRUNCATE refund_lines",
    ):
        state, _ = expect_fail(conn, sql)
        assert state == "23001", sql


def test_line_level_drift_is_detected(conn: Connection) -> None:
    c = make_customer(conn)
    o = make_order(conn, c, items=[6000, 4000])
    big, _ = item_ids(conn, o)
    make_refund(conn, c, o, _action(conn, c, o, 3000), 3000, lines=[(big, 3000)])
    assert find_refund_line_drift(conn) == []
    conn.execute(text("UPDATE order_items SET refunded_cents = 0 WHERE id = :i"), {"i": big})
    drift = find_refund_line_drift(conn)
    assert [(d.level, d.recorded_cents, d.summed_cents) for d in drift] == [("item_total", 0, 3000)]
    try:
        assert_no_refund_drift(conn)
    except RefundDriftError:
        return
    raise AssertionError("line-level drift was not reported by assert_no_refund_drift")


def test_order_total_vs_lines_drift_is_detected(conn: Connection) -> None:
    c = make_customer(conn)
    o = make_order(conn, c)
    make_refund(conn, c, o, _action(conn, c, o, 3000), 3000)
    conn.execute(text("UPDATE orders SET refunded_total_cents = 1000 WHERE id = :o"), {"o": o})
    levels = {d.level for d in find_refund_line_drift(conn)}
    assert "order_vs_lines" in levels


def test_without_security_invoker_row_level_security_hides_drift_from_the_check(
    conn: Connection,
) -> None:
    """F-010. The same drift as above, but with the view's security_invoker option removed: the view
    then runs as its owner, who is subject to FORCE ROW LEVEL SECURITY and sees no rows, so the
    invariant check reports 'no drift' about a database that has drifted. Rolled back with the test."""
    c = make_customer(conn)
    o = make_order(conn, c, items=[6000, 4000])
    big, _ = item_ids(conn, o)
    make_refund(conn, c, o, _action(conn, c, o, 3000), 3000, lines=[(big, 3000)])
    conn.execute(text("UPDATE order_items SET refunded_cents = 0 WHERE id = :i"), {"i": big})
    assert find_refund_line_drift(conn) != []  # visible with the option set (migration 0012)
    conn.execute(text("ALTER VIEW v_refund_line_drift RESET (security_invoker)"))
    assert find_refund_line_drift(conn) == []  # the silent failure the option prevents


def test_every_view_in_the_schema_is_security_invoker(conn: Connection) -> None:
    """A view over a protected table without the option silently returns only the owner's (empty)
    scope. A new view must opt in, or this fails (F-010)."""
    views = conn.execute(
        text(
            "SELECT c.relname, coalesce(c.reloptions::text, '') AS opts FROM pg_class c"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'public' AND c.relkind = 'v'"
        )
    ).all()
    assert views, "expected the drift views"
    assert [v.relname for v in views if "security_invoker=true" not in v.opts] == []
