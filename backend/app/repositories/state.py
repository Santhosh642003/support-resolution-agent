"""Read-only queries for live order, item, shipment and subscription state.

EVERY query takes a CustomerContext and filters `customer_id = :c` itself. There is no function
here that takes a customer id from anywhere else, and none that can read another customer's row.
"Not found" and "not yours" are the same result (None / empty), so callers cannot tell them apart.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import Connection, Row, text

from app.auth.context import CustomerContext

_ORDER_COLS = (
    "o.id, o.order_number, o.kind, o.status, o.purchase_date, o.list_total_cents,"
    " o.discount_cents, o.shipping_cents, o.paid_cents, o.refunded_total_cents,"
    " o.subscription_id, orig.order_number AS replacement_for_number"
)


def list_orders(
    conn: Connection, ctx: CustomerContext, limit: int = 50
) -> list[Row[tuple[object, ...]]]:
    return list(
        conn.execute(
            text(
                f"SELECT {_ORDER_COLS}, s.status AS shipment_status"  # noqa: S608 (constants)
                " FROM orders o"
                " LEFT JOIN orders orig ON orig.id = o.replacement_for_order_id"
                "   AND orig.customer_id = o.customer_id"
                " LEFT JOIN shipments s ON s.order_id = o.id AND s.customer_id = o.customer_id"
                " WHERE o.customer_id = :c ORDER BY o.placed_at DESC, o.order_number LIMIT :n"
            ),
            {"c": ctx.customer_id, "n": limit},
        )
    )


def get_order(
    conn: Connection, ctx: CustomerContext, order_number: str
) -> Row[tuple[object, ...]] | None:
    return conn.execute(
        text(
            f"SELECT {_ORDER_COLS} FROM orders o"  # noqa: S608 (constants)
            " LEFT JOIN orders orig ON orig.id = o.replacement_for_order_id"
            "   AND orig.customer_id = o.customer_id"
            " WHERE o.customer_id = :c AND o.order_number = :n"
        ),
        {"c": ctx.customer_id, "n": order_number},
    ).one_or_none()


def replacements_of(conn: Connection, ctx: CustomerContext, order_id: UUID) -> list[str]:
    rows = conn.execute(
        text(
            "SELECT order_number FROM orders"
            " WHERE customer_id = :c AND replacement_for_order_id = :o ORDER BY placed_at"
        ),
        {"c": ctx.customer_id, "o": order_id},
    )
    return [r.order_number for r in rows]


def items_of(
    conn: Connection, ctx: CustomerContext, order_id: UUID
) -> list[Row[tuple[object, ...]]]:
    return list(
        conn.execute(
            text(
                "SELECT p.sku, p.name, p.is_digital_addon, i.qty, i.unit_price_cents,"
                " i.paid_cents, i.refunded_cents, i.final_sale, i.digital_accessed_at"
                " FROM order_items i JOIN products p ON p.id = i.product_id"
                " WHERE i.customer_id = :c AND i.order_id = :o ORDER BY p.sku, i.id"
            ),
            {"c": ctx.customer_id, "o": order_id},
        )
    )


def categories_of_order(conn: Connection, ctx: CustomerContext, order_id: UUID) -> set[str]:
    """Product categories present on one of THIS customer's orders (a structured scoping fact)."""
    rows = conn.execute(
        text(
            "SELECT DISTINCT p.category FROM order_items i JOIN products p ON p.id = i.product_id"
            " WHERE i.customer_id = :c AND i.order_id = :o"
        ),
        {"c": ctx.customer_id, "o": order_id},
    )
    return {r.category for r in rows}


def shipment_of(
    conn: Connection, ctx: CustomerContext, order_id: UUID
) -> Row[tuple[object, ...]] | None:
    return conn.execute(
        text(
            "SELECT id, carrier, tracking_no, status, shipped_at, expected_at, delivered_at"
            " FROM shipments WHERE customer_id = :c AND order_id = :o"
        ),
        {"c": ctx.customer_id, "o": order_id},
    ).one_or_none()


def shipment_events(
    conn: Connection, ctx: CustomerContext, shipment_id: UUID
) -> list[Row[tuple[object, ...]]]:
    return list(
        conn.execute(
            text(
                "SELECT occurred_at, code, detail FROM shipment_events"
                " WHERE customer_id = :c AND shipment_id = :s ORDER BY occurred_at, id"
            ),
            {"c": ctx.customer_id, "s": shipment_id},
        )
    )


def list_subscriptions(conn: Connection, ctx: CustomerContext) -> list[Row[tuple[object, ...]]]:
    return list(
        conn.execute(
            text(
                "SELECT id, plan_type, status, started_at, first_charge_at, first_charge_cents,"
                " price_cents, current_period_start, current_period_end, canceled_at,"
                " cancel_effective_at FROM subscriptions WHERE customer_id = :c"
                " ORDER BY started_at DESC, id"
            ),
            {"c": ctx.customer_id},
        )
    )


def get_subscription(
    conn: Connection, ctx: CustomerContext, subscription_id: UUID
) -> Row[tuple[object, ...]] | None:
    return conn.execute(
        text(
            "SELECT id, plan_type, status, started_at, first_charge_at, first_charge_cents,"
            " price_cents, current_period_start, current_period_end, canceled_at,"
            " cancel_effective_at FROM subscriptions WHERE customer_id = :c AND id = :i"
        ),
        {"c": ctx.customer_id, "i": subscription_id},
    ).one_or_none()


@dataclass(frozen=True)
class ShipmentSnapshot:
    id: UUID
    status: str
    shipped_at: datetime | None
    expected_at: datetime | None
    delivered_at: datetime | None


@dataclass(frozen=True)
class OrderSnapshot:
    """One order and everything the order-status rules read about it, from ONE statement."""

    order: Row[tuple[object, ...]]
    shipment: ShipmentSnapshot | None
    event_codes: tuple[str, ...]
    categories: frozenset[str]


def order_snapshot(
    conn: Connection, ctx: CustomerContext, order_number: str
) -> OrderSnapshot | None:
    """The order, its shipment, its shipment event codes and its product categories as one
    CONSISTENT view. Four separate reads could observe four different states if a row changed
    between them; a single statement sees one snapshot, which matters more once writes exist.
    Still filters `customer_id = :c` on every table, as the second control behind RLS."""
    row = conn.execute(
        text(
            f"SELECT {_ORDER_COLS},"  # noqa: S608 (constants)
            " s.id AS ship_id, s.status AS ship_status, s.shipped_at, s.expected_at,"
            " s.delivered_at,"
            " ARRAY(SELECT e.code FROM shipment_events e WHERE e.customer_id = :c"
            "   AND e.shipment_id = s.id ORDER BY e.occurred_at, e.id) AS event_codes,"
            " ARRAY(SELECT DISTINCT p.category FROM order_items i"
            "   JOIN products p ON p.id = i.product_id"
            "   WHERE i.customer_id = :c AND i.order_id = o.id) AS categories"
            " FROM orders o"
            " LEFT JOIN orders orig ON orig.id = o.replacement_for_order_id"
            "   AND orig.customer_id = o.customer_id"
            " LEFT JOIN shipments s ON s.order_id = o.id AND s.customer_id = o.customer_id"
            " WHERE o.customer_id = :c AND o.order_number = :n"
        ),
        {"c": ctx.customer_id, "n": order_number},
    ).one_or_none()
    if row is None:
        return None
    ship = (
        None
        if row.ship_id is None
        else ShipmentSnapshot(
            row.ship_id, str(row.ship_status), row.shipped_at, row.expected_at, row.delivered_at
        )
    )
    return OrderSnapshot(
        order=row,
        shipment=ship,
        event_codes=tuple(row.event_codes or ()),
        categories=frozenset(row.categories or ()),
    )
