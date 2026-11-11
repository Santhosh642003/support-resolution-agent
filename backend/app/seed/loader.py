"""Idempotent seeding of the synthetic dataset.

    uv run python -m app.seed

Running it twice changes nothing: every row has a deterministic id derived from its key, is
inserted with ON CONFLICT DO NOTHING, and, if it already exists, is compared with the
definition. A difference raises (like ingestion does for policy text) instead of silently
leaving stale or hand-edited data. Columns the live system legitimately changes (statuses,
cancellation timestamps, accessed-at) are not compared, so re-seeding after real activity does
not fail; refund totals are never written here.

The whole run happens in the caller's transaction. It finishes by running
assert_no_refund_drift(); if the invariant trips, the exception propagates and the caller's
transaction rolls back, so a drifted seed is never committed.
"""

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, text

from app.db.invariants import assert_no_refund_drift
from app.seed import ids
from app.seed.definitions import (
    CARRIER,
    CUSTOMERS,
    ORDERS,
    PLACEHOLDER_PASSWORD_HASH,
    PRODUCTS,
    SUBSCRIPTIONS,
    OrderDef,
)


class SeedChangedError(RuntimeError):
    """A seeded row exists but differs from the seed definition."""


class SeedConflictError(RuntimeError):
    """A seed row collides with an unrelated existing row on a unique key (e.g. the email)."""


@dataclass(frozen=True)
class SeedReport:
    inserted: dict[str, int]
    unchanged: int

    @property
    def total_inserted(self) -> int:
        return sum(self.inserted.values())


# Columns the running system may legitimately change after seeding; excluded from comparison.
_STATE_COLUMNS: dict[str, frozenset[str]] = {
    "subscriptions": frozenset({"status", "canceled_at", "cancel_effective_at"}),
    "orders": frozenset({"status"}),
    "order_items": frozenset({"digital_accessed_at"}),
    "shipments": frozenset({"status", "delivered_at"}),
}


class _Seeder:
    def __init__(self, conn: Connection) -> None:
        self.conn = conn
        self.inserted: Counter[str] = Counter()
        self.unchanged = 0

    def ensure(self, table: str, row: Mapping[str, Any]) -> bool:
        """Insert the row if absent. Returns True if inserted. Raises if present but different."""
        cols = list(row)
        # Table and column names come from constants in this module, never from input.
        res = self.conn.execute(
            text(
                f"INSERT INTO {table} ({', '.join(cols)})"  # noqa: S608
                f" VALUES ({', '.join(':' + c for c in cols)}) ON CONFLICT DO NOTHING"
            ),
            dict(row),
        )
        if res.rowcount == 1:
            self.inserted[table] += 1
            return True
        compare = [c for c in cols if c not in _STATE_COLUMNS.get(table, frozenset())]
        existing = (
            self.conn.execute(
                text(f"SELECT {', '.join(compare)} FROM {table} WHERE id = :id"),  # noqa: S608
                {"id": row["id"]},
            )
            .mappings()
            .one_or_none()
        )
        if existing is None:
            raise SeedConflictError(
                f"{table} id {row['id']} was not inserted and does not exist: the row collides "
                "with a different row on a unique column"
            )
        diffs = {c: (existing[c], row[c]) for c in compare if existing[c] != row[c]}
        if diffs:
            raise SeedChangedError(
                f"{table} {row['id']} differs from its seed definition (stored, defined): {diffs}"
            )
        self.unchanged += 1
        return False

    def shipment(self, order: OrderDef) -> None:
        ship = order.shipment
        if ship is None:
            raise ValueError(f"order {order.key} has no shipment definition")
        sid = ids.shipment_id(order.key)
        created = self.ensure(
            "shipments",
            {
                "id": sid,
                "order_id": ids.order_id(order.key),
                "customer_id": ids.customer_id(order.customer),
                "carrier": CARRIER,
                "tracking_no": ship.tracking_no,
                "status": ship.status,
                "shipped_at": ship.shipped_at,
                "delivered_at": ship.delivered_at,
                "expected_at": ship.expected_at,
            },
        )
        want = [(e.at, e.code, e.detail) for e in ship.events]
        if created:
            for e in ship.events:
                self.conn.execute(
                    text(
                        "INSERT INTO shipment_events (shipment_id, customer_id, occurred_at, code,"
                        " detail) VALUES (:s, :c, :at, :code, :detail)"
                    ),
                    {
                        "s": sid,
                        "c": ids.customer_id(order.customer),
                        "at": e.at,
                        "code": e.code,
                        "detail": e.detail,
                    },
                )
            self.inserted["shipment_events"] += len(want)
            return
        have = [
            (r.occurred_at, r.code, r.detail)
            for r in self.conn.execute(
                text(
                    "SELECT occurred_at, code, detail FROM shipment_events"
                    " WHERE shipment_id = :s ORDER BY occurred_at, id"
                ),
                {"s": sid},
            )
        ]
        if have != want:
            raise SeedChangedError(f"shipment_events for {order.key} differ from the definition")
        self.unchanged += len(want)


def seed_all(conn: Connection) -> SeedReport:
    """Seed customers, products, subscriptions, orders, items, shipments and events, then
    assert the refund invariants. Call inside a transaction."""
    # Serialize concurrent seed runs (two runs could otherwise race on the event inserts).
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('seed_data'))"))
    s = _Seeder(conn)

    for c in CUSTOMERS:
        s.ensure(
            "customers",
            {
                "id": ids.customer_id(c.key),
                "email": c.email,
                "display_name": c.display_name,
                "password_hash": PLACEHOLDER_PASSWORD_HASH,
                "created_at": c.created_at,
            },
        )
    for p in PRODUCTS:
        s.ensure(
            "products",
            {
                "id": ids.product_id(p.sku),
                "sku": p.sku,
                "name": p.name,
                "price_cents": p.price_cents,
                "is_digital_addon": p.is_digital_addon,
            },
        )
    for sub in SUBSCRIPTIONS:
        s.ensure(
            "subscriptions",
            {
                "id": ids.subscription_id(sub.key),
                "customer_id": ids.customer_id(sub.customer),
                "plan_type": sub.plan_type,
                "status": sub.status,
                "started_at": sub.started_at,
                "first_charge_at": sub.first_charge_at,
                "first_charge_cents": sub.first_charge_cents,
                "price_cents": sub.price_cents,
                "current_period_start": sub.current_period_start,
                "current_period_end": sub.current_period_end,
            },
        )
    # ORDERS is ordered so every referenced order (replacement_for) precedes its referrer.
    for o in ORDERS:
        s.ensure(
            "orders",
            {
                "id": ids.order_id(o.key),
                "order_number": o.order_number,
                "customer_id": ids.customer_id(o.customer),
                "kind": o.kind,
                "subscription_id": ids.subscription_id(o.subscription) if o.subscription else None,
                "replacement_for_order_id": (
                    ids.order_id(o.replacement_for) if o.replacement_for else None
                ),
                "placed_at": o.placed_at,
                "status": o.status,
                "list_total_cents": o.list_total_cents,
                "discount_cents": o.discount_cents,
                "shipping_cents": o.shipping_cents,
                "paid_cents": o.paid_cents,
            },
        )
        for i, item in enumerate(o.items):
            s.ensure(
                "order_items",
                {
                    "id": ids.item_id(o.key, i),
                    "order_id": ids.order_id(o.key),
                    "customer_id": ids.customer_id(o.customer),
                    "product_id": ids.product_id(item.sku),
                    "qty": item.qty,
                    "unit_price_cents": item.unit_price_cents,
                    "paid_cents": item.paid_cents,
                    "final_sale": item.final_sale,
                    "digital_accessed_at": item.digital_accessed_at,
                },
            )
        if o.shipment is not None:
            s.shipment(o)

    assert_no_refund_drift(conn)  # raises (and the caller's transaction rolls back) on drift
    return SeedReport(dict(s.inserted), s.unchanged)
