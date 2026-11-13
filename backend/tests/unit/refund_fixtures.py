"""Builders for refund-rule inputs: from the seed definitions (the eight cases are the real seed) and
from keyword arguments (boundary tests). No database, no clock."""

from datetime import date, datetime, timedelta

from app.domain.rules.refund import RefundItem, RefundOrder, RefundShipment
from app.domain.types import PurchaseDate
from app.seed.definitions import ORDERS, PRODUCTS, OrderDef

PRODUCT = {p.sku: p for p in PRODUCTS}
ORDER = {o.key: o for o in ORDERS}
NUMBER = {o.order_number: o for o in ORDERS}


def day(ts: datetime) -> date:
    return ts.date()  # the seed instants are noon UTC, so the UTC calendar date is unambiguous


def from_seed(key_or_number: str) -> RefundOrder:
    o: OrderDef = ORDER.get(key_or_number) or NUMBER[key_or_number]
    items = tuple(
        RefundItem(
            item_id=f"{o.order_number}:{n}",
            sku=i.sku,
            category=PRODUCT[i.sku].category,
            paid_cents=i.paid_cents,
            final_sale=i.final_sale,
            digital_addon=PRODUCT[i.sku].is_digital_addon,
            digital_accessed_on=None
            if i.digital_accessed_at is None
            else day(i.digital_accessed_at),
        )
        for n, i in enumerate(o.items)
    )
    ship = None
    if o.shipment is not None:
        s = o.shipment
        ship = RefundShipment(
            status=s.status,
            shipped_on=day(s.shipped_at),
            delivered_on=None if s.delivered_at is None else day(s.delivered_at),
            has_delivery_exception=any(e.code == "DELIVERY_EXCEPTION" for e in s.events),
        )
    replaced = any(x.replacement_for == o.key for x in ORDERS)
    return RefundOrder(
        order_number=o.order_number,
        purchase_date=PurchaseDate(day(o.placed_at)),
        status=o.status,
        items=items,
        shipment=ship,
        kind=o.kind,
        list_total_cents=o.list_total_cents,
        discount_cents=o.discount_cents,
        shipping_cents=o.shipping_cents,
        paid_cents=o.paid_cents,
        has_replacement=replaced,
    )


def delivered_order(
    purchase: date,
    delivered: date,
    *amounts: int,
    discount: int = 0,
    shipping: int = 0,
    **item_kw: object,
) -> RefundOrder:
    """A delivered order with one item per amount (paid cents)."""
    items = tuple(
        RefundItem(f"i{n}", f"SKU{n}", "general", amt, **item_kw)  # type: ignore[arg-type]
        for n, amt in enumerate(amounts)
    )
    paid = sum(amounts) + shipping
    return RefundOrder(
        order_number="T-1",
        purchase_date=PurchaseDate(purchase),
        status="delivered",
        items=items,
        shipment=RefundShipment("delivered", delivered - timedelta(days=3), delivered),
        list_total_cents=sum(amounts) + discount,
        discount_cents=discount,
        shipping_cents=shipping,
        paid_cents=paid,
    )
