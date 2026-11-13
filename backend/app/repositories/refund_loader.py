"""Database rows to the refund rules' input (SPEC 18.5): `load_refund_order`.

    live rows --(this module)--> RefundOrder --(decide_refund, pure)--> RefundDecision

Everything the rules read comes from ONE statement, so the order, its lines, its shipment and its
replacements are one consistent snapshot (as `order_snapshot` does for order status). Every table in
it is filtered by `customer_id = :c` from the authenticated context, as the second control behind
row-level security; "not found" and "not yours" are the same result (None).

What is read, and how it maps:
  * lines: ALL `order_items` rows, item lines and the shipping line (`line_type = 'shipping'`, which
    has no product, so its sku is SHIPPING and its category `shipping`), with the running
    `refunded_cents` per line and the box counts (`items_total`, `items_shipped`);
  * the order's shipment and whether a carrier DELIVERY_EXCEPTION event exists;
  * linked replacement orders (`orders.replacement_for_order_id`), each with its own shipment, so
    the replacement rule can measure the replacement's OWN window;
  * dates are UTC calendar dates, the same convention as `orders.purchase_date`.
The model never touches this: the order number comes from extraction, the customer from the session.
"""

from datetime import date
from typing import Any

from sqlalchemy import Connection, text

from app.auth.context import CustomerContext
from app.domain.rules.refund import (
    RefundItem,
    RefundOrder,
    RefundReplacement,
    RefundShipment,
)
from app.domain.types import PurchaseDate

_SHIPMENT_JSON = (
    "(SELECT json_build_object("
    "   'status', s.status,"
    "   'shipped_on', (s.shipped_at AT TIME ZONE 'UTC')::date,"
    "   'delivered_on', (s.delivered_at AT TIME ZONE 'UTC')::date,"
    "   'exception', EXISTS (SELECT 1 FROM shipment_events e WHERE e.customer_id = :c"
    "                         AND e.shipment_id = s.id AND e.code = 'DELIVERY_EXCEPTION'))"
    " FROM shipments s WHERE s.customer_id = :c AND s.order_id = {order_alias}.id)"
)

# The pieces are module constants; every value is a bind parameter.
_SQL = text(
    "SELECT o.order_number, o.purchase_date, o.status, o.kind, o.list_total_cents,"  # noqa: S608
    " o.discount_cents, o.shipping_cents, o.paid_cents, o.refunded_total_cents,"
    " " + _SHIPMENT_JSON.format(order_alias="o") + " AS shipment,"
    " COALESCE((SELECT json_agg(json_build_object("
    "     'id', i.id, 'line_type', i.line_type, 'sku', p.sku, 'category', p.category,"
    "     'digital_addon', COALESCE(p.is_digital_addon, false), 'paid_cents', i.paid_cents,"
    "     'refunded_cents', i.refunded_cents, 'final_sale', i.final_sale,"
    "     'digital_accessed_on', (i.digital_accessed_at AT TIME ZONE 'UTC')::date,"
    "     'items_total', i.items_total, 'items_shipped', i.items_shipped)"
    "   ORDER BY (i.line_type = 'shipping'), p.sku, i.id)"
    "   FROM order_items i LEFT JOIN products p ON p.id = i.product_id"
    "   WHERE i.customer_id = :c AND i.order_id = o.id), '[]'::json) AS lines,"
    " COALESCE((SELECT json_agg(json_build_object("
    "     'order_number', r.order_number, 'status', r.status,"
    "     'shipment', " + _SHIPMENT_JSON.format(order_alias="r") + ")"
    "   ORDER BY r.placed_at, r.order_number)"
    "   FROM orders r WHERE r.customer_id = :c AND r.replacement_for_order_id = o.id),"
    "   '[]'::json) AS replacements"
    " FROM orders o WHERE o.customer_id = :c AND o.order_number = :n"
)


def _day(value: Any) -> date | None:
    return None if value is None else date.fromisoformat(str(value))


def _shipment(blob: dict[str, Any] | None) -> RefundShipment | None:
    if blob is None:
        return None
    return RefundShipment(
        status=str(blob["status"]),
        shipped_on=_day(blob["shipped_on"]),
        delivered_on=_day(blob["delivered_on"]),
        has_delivery_exception=bool(blob["exception"]),
    )


def load_refund_order(
    conn: Connection, ctx: CustomerContext, order_number: str
) -> RefundOrder | None:
    """The customer's order as the refund rules need it, or None if it is not theirs / absent."""
    row = conn.execute(_SQL, {"c": ctx.customer_id, "n": order_number}).one_or_none()
    if row is None:
        return None
    items = tuple(
        RefundItem(
            item_id=str(line["id"]),
            sku=line["sku"] or "SHIPPING",
            category=line["category"] or "shipping",
            paid_cents=int(line["paid_cents"]),
            refunded_cents=int(line["refunded_cents"]),
            final_sale=bool(line["final_sale"]),
            digital_addon=bool(line["digital_addon"]),
            digital_accessed_on=_day(line["digital_accessed_on"]),
            items_total=line["items_total"],
            items_shipped=line["items_shipped"],
            line_type=str(line["line_type"]),
        )
        for line in row.lines
    )
    replacements = tuple(
        RefundReplacement(str(r["order_number"]), str(r["status"]), _shipment(r["shipment"]))
        for r in row.replacements
    )
    return RefundOrder(
        order_number=row.order_number,
        purchase_date=PurchaseDate(row.purchase_date),
        status=row.status,
        items=items,
        shipment=_shipment(row.shipment),
        kind=row.kind,
        list_total_cents=int(row.list_total_cents),
        discount_cents=int(row.discount_cents),
        shipping_cents=int(row.shipping_cents),
        paid_cents=int(row.paid_cents),
        refunded_total_cents=int(row.refunded_total_cents),
        replacements=replacements,
    )
