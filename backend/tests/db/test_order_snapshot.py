"""get_order_snapshot is one statement and equals the separate reads it replaced."""

from sqlalchemy import Connection, event

from app.auth.context import CustomerContext
from app.repositories import state as repo
from app.seed import ids
from app.seed.loader import seed_all


def test_the_snapshot_equals_the_separate_reads_for_every_seeded_order(conn: Connection) -> None:
    seed_all(conn)
    checked = 0
    for who in ("alex", "blair", "carol"):
        ctx = CustomerContext(ids.customer_id(who))
        for o in repo.list_orders(conn, ctx):
            snap = repo.order_snapshot(conn, ctx, o.order_number)
            assert snap is not None and snap.order.id == o.id
            ship = repo.shipment_of(conn, ctx, o.id)
            assert (snap.shipment is None) == (ship is None)
            if ship is not None and snap.shipment is not None:
                assert (snap.shipment.id, snap.shipment.status) == (ship.id, str(ship.status))
                assert snap.shipment.shipped_at == ship.shipped_at
                assert snap.shipment.delivered_at == ship.delivered_at
                assert list(snap.event_codes) == [
                    e.code for e in repo.shipment_events(conn, ctx, ship.id)
                ]
            assert snap.categories == repo.categories_of_order(conn, ctx, o.id)
            checked += 1
    assert checked >= 6


def test_the_snapshot_is_a_single_statement_so_it_is_one_consistent_view(conn: Connection) -> None:
    seed_all(conn)
    ctx = CustomerContext(ids.customer_id("blair"))
    seen: list[str] = []

    def count(_c: object, _cur: object, statement: str, *_a: object) -> None:
        seen.append(statement)

    event.listen(conn, "before_cursor_execute", count)
    try:
        assert repo.order_snapshot(conn, ctx, "SIM-2001") is not None
    finally:
        event.remove(conn, "before_cursor_execute", count)
    assert len(seen) == 1


def test_another_customers_order_and_a_missing_one_are_the_same_none(conn: Connection) -> None:
    seed_all(conn)
    alex = CustomerContext(ids.customer_id("alex"))
    assert repo.order_snapshot(conn, alex, "SIM-2001") is None  # blair's order
    assert repo.order_snapshot(conn, alex, "SIM-9999") is None
