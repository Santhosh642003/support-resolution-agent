"""Read-only state endpoints: the live-data path the agent reads instead of embeddings.

No route takes a customer id (path, query or body). Identity is `current_customer`, resolved from
the session cookie. Every derived date comes from the injected clock. A record that does not exist
and a record that belongs to someone else produce the identical 404.
"""

from datetime import UTC, date, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import Connection

from app.api import schemas as s
from app.api.deps import current_customer, get_clock, get_conn, not_found
from app.auth.context import CustomerContext
from app.clock import Clock
from app.domain.rules.shipping import expected_delivery_date, presumed_lost_date
from app.repositories import state as repo

router = APIRouter()

Conn = Annotated[Connection, Depends(get_conn)]
Customer = Annotated[CustomerContext, Depends(current_customer)]
Now = Annotated[Clock, Depends(get_clock)]


def _on(ts: datetime | None) -> date | None:
    """The business (UTC calendar) date of a stored instant."""
    return None if ts is None else ts.astimezone(UTC).date()


def _meta(clock: Clock) -> dict[str, Any]:
    return {"as_of": clock.today(), "data_as_of": clock.now(), "simulated": True}


def _item(r: Any) -> s.OrderItemOut:
    return s.OrderItemOut(
        sku=r.sku,
        name=r.name,
        qty=r.qty,
        unit_price_cents=r.unit_price_cents,
        paid_cents=r.paid_cents,
        refunded_cents=r.refunded_cents,
        final_sale=r.final_sale,
        is_digital_addon=r.is_digital_addon,
        digital_accessed_on=_on(r.digital_accessed_at),
    )


def _shipment(conn: Connection, ctx: CustomerContext, clock: Clock, ship: Any) -> s.ShipmentOut:
    today = clock.today()
    shipped_on = _on(ship.shipped_at)
    delivered_on = _on(ship.delivered_at)
    lost_on = presumed_lost_date(shipped_on) if shipped_on else None
    events = repo.shipment_events(conn, ctx, ship.id)
    return s.ShipmentOut(
        **_meta(clock),
        tracking_no=ship.tracking_no,
        carrier=ship.carrier,
        status=ship.status,
        shipped_on=shipped_on,
        delivered_on=delivered_on,
        carrier_expected_on=_on(ship.expected_at),
        policy_expected_on=expected_delivery_date(shipped_on) if shipped_on else None,
        presumed_lost_on=lost_on,
        days_since_shipped=(today - shipped_on).days if shipped_on else None,
        is_presumed_lost=delivered_on is None and lost_on is not None and today >= lost_on,
        has_delivery_exception=ship.status == "exception"
        or any(e.code == "DELIVERY_EXCEPTION" for e in events),
        events=tuple(
            s.ShipmentEventOut(occurred_on=_on(e.occurred_at), code=e.code, detail=e.detail)  # type: ignore[arg-type]
            for e in events
        ),
    )


def _subscription(clock: Clock, r: Any) -> s.SubscriptionOut:
    first = _on(r.first_charge_at)
    return s.SubscriptionOut(
        **_meta(clock),
        subscription_id=str(r.id),
        plan_type=r.plan_type,
        status=r.status,
        started_on=_on(r.started_at),  # type: ignore[arg-type]
        first_charge_on=first,  # type: ignore[arg-type]
        first_charge_cents=r.first_charge_cents,
        price_cents=r.price_cents,
        current_period_start_on=_on(r.current_period_start),  # type: ignore[arg-type]
        current_period_end_on=_on(r.current_period_end),  # type: ignore[arg-type]
        canceled_on=_on(r.canceled_at),
        cancel_effective_on=_on(r.cancel_effective_at),
        days_since_first_charge=(clock.today() - first).days,  # type: ignore[operator]
    )


@router.get("/orders", response_model=s.OrderListOut)
def list_orders(conn: Conn, ctx: Customer, clock: Now) -> s.OrderListOut:
    rows = repo.list_orders(conn, ctx)
    return s.OrderListOut(
        **_meta(clock),
        orders=tuple(
            s.OrderSummaryOut(
                order_number=r.order_number,
                kind=r.kind,
                status=r.status,
                placed_on=r.purchase_date,
                paid_cents=r.paid_cents,
                refunded_total_cents=r.refunded_total_cents,
                shipment_status=r.shipment_status,
            )
            for r in rows
        ),
    )


@router.get("/orders/{order_number}", response_model=s.OrderOut)
def get_order(order_number: str, conn: Conn, ctx: Customer, clock: Now) -> s.OrderOut:
    order = repo.get_order(conn, ctx, order_number)
    if order is None:
        raise not_found()
    ship = repo.shipment_of(conn, ctx, order.id)
    return s.OrderOut(
        **_meta(clock),
        order_number=order.order_number,
        kind=order.kind,
        status=order.status,
        placed_on=order.purchase_date,
        list_total_cents=order.list_total_cents,
        discount_cents=order.discount_cents,
        shipping_cents=order.shipping_cents,
        paid_cents=order.paid_cents,
        refunded_total_cents=order.refunded_total_cents,
        refundable_remaining_cents=order.paid_cents - order.refunded_total_cents,
        replacement_for_order_number=order.replacement_for_number,
        replaced_by_order_numbers=tuple(repo.replacements_of(conn, ctx, order.id)),
        subscription_id=str(order.subscription_id) if order.subscription_id else None,
        items=tuple(_item(r) for r in repo.items_of(conn, ctx, order.id)),
        shipment=_shipment(conn, ctx, clock, ship) if ship else None,
    )


@router.get("/orders/{order_number}/items", response_model=s.OrderItemsOut)
def get_order_items(order_number: str, conn: Conn, ctx: Customer, clock: Now) -> s.OrderItemsOut:
    order = repo.get_order(conn, ctx, order_number)
    if order is None:
        raise not_found()
    return s.OrderItemsOut(
        **_meta(clock),
        order_number=order.order_number,
        items=tuple(_item(r) for r in repo.items_of(conn, ctx, order.id)),
    )


@router.get("/orders/{order_number}/shipment", response_model=s.ShipmentOut)
def get_order_shipment(order_number: str, conn: Conn, ctx: Customer, clock: Now) -> s.ShipmentOut:
    order = repo.get_order(conn, ctx, order_number)
    ship = repo.shipment_of(conn, ctx, order.id) if order is not None else None
    if order is None or ship is None:  # an order with no shipment is the same 404
        raise not_found()
    return _shipment(conn, ctx, clock, ship)


@router.get("/subscriptions", response_model=s.SubscriptionListOut)
def list_subscriptions(conn: Conn, ctx: Customer, clock: Now) -> s.SubscriptionListOut:
    return s.SubscriptionListOut(
        **_meta(clock),
        subscriptions=tuple(_subscription(clock, r) for r in repo.list_subscriptions(conn, ctx)),
    )


@router.get("/subscriptions/{subscription_id}", response_model=s.SubscriptionOut)
def get_subscription(
    subscription_id: str, conn: Conn, ctx: Customer, clock: Now
) -> s.SubscriptionOut:
    try:
        sid = UUID(subscription_id)
    except ValueError:
        raise not_found() from None  # a malformed id is indistinguishable from a missing one
    row = repo.get_subscription(conn, ctx, sid)
    if row is None:
        raise not_found()
    return _subscription(clock, row)
