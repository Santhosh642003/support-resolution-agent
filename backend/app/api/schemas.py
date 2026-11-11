"""Typed response schemas for the read-only state API (SIMULATED DATA).

Deliberately absent: any customer id (in a response, a path, a query or a body). Identity comes
from the session. Every date is a business `date` (the UTC calendar date), derived from stored
instants and from the injected Clock; there are no raw database rows or ORM objects here.
"""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True)


class StateMeta(_Out):
    as_of: date  # the business date these facts were derived for (the injected clock)
    data_as_of: datetime  # the instant the data was read, from the injected clock
    simulated: bool = True  # all data in this project is synthetic


class ShipmentEventOut(_Out):
    occurred_on: date
    code: str
    detail: str | None


class ShipmentOut(StateMeta):
    tracking_no: str
    carrier: str
    status: Literal["label_created", "in_transit", "delivered", "exception", "lost"]
    shipped_on: date | None
    delivered_on: date | None
    carrier_expected_on: date | None  # what the carrier said
    policy_expected_on: date | None  # 7 business days from the ship date (shipping policy)
    presumed_lost_on: date | None  # 10 business days from the ship date (shipping policy)
    days_since_shipped: int | None
    is_presumed_lost: bool  # undelivered and as_of is on/after presumed_lost_on
    has_delivery_exception: bool
    events: tuple[ShipmentEventOut, ...]


class OrderItemOut(_Out):
    sku: str
    name: str
    qty: int
    unit_price_cents: int
    paid_cents: int  # what was actually paid for the line (after discount), never list price
    refunded_cents: int
    final_sale: bool
    is_digital_addon: bool
    digital_accessed_on: date | None


class OrderSummaryOut(_Out):
    order_number: str
    kind: Literal["purchase", "subscription_charge"]
    status: Literal["placed", "shipped", "delivered", "canceled"]
    placed_on: date  # the purchase date: selects the policy version
    paid_cents: int
    refunded_total_cents: int
    shipment_status: str | None


class OrderListOut(StateMeta):
    orders: tuple[OrderSummaryOut, ...]


class OrderOut(StateMeta):
    order_number: str
    kind: Literal["purchase", "subscription_charge"]
    status: Literal["placed", "shipped", "delivered", "canceled"]
    placed_on: date
    list_total_cents: int
    discount_cents: int
    shipping_cents: int
    paid_cents: int
    refunded_total_cents: int
    refundable_remaining_cents: int  # paid minus refunded: the paid-amount invariant's headroom
    replacement_for_order_number: str | None
    replaced_by_order_numbers: tuple[str, ...]
    subscription_id: str | None
    items: tuple[OrderItemOut, ...]
    shipment: ShipmentOut | None


class OrderItemsOut(StateMeta):
    order_number: str
    items: tuple[OrderItemOut, ...]


class SubscriptionOut(StateMeta):
    subscription_id: str
    plan_type: Literal["monthly", "annual"]
    status: Literal["active", "cancel_scheduled", "canceled"]
    started_on: date
    first_charge_on: date
    first_charge_cents: int
    price_cents: int
    current_period_start_on: date
    current_period_end_on: date
    canceled_on: date | None
    cancel_effective_on: date | None
    days_since_first_charge: int  # as_of minus the first-charge date


class SubscriptionListOut(StateMeta):
    subscriptions: tuple[SubscriptionOut, ...]
