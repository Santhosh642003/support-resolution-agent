"""Cross-customer tests against EVERY repository function (SPEC 2.2, adversarial family 1).

The API layer checks ownership of the order first, so a missing filter deeper down would not show
up through the API. Each function must therefore refuse another customer's ids by itself."""

import pytest
from sqlalchemy import Connection, text

from app.auth.context import CustomerContext
from app.repositories import state as repo
from app.seed import ids
from app.seed.loader import seed_all


@pytest.fixture
def seeded(conn: Connection) -> Connection:
    seed_all(conn)
    return conn


ALEX = CustomerContext(ids.customer_id("alex"))
BLAIR = CustomerContext(ids.customer_id("blair"))
BLAIR_ORDER = ids.order_id("lost_shipment_with_replacement_sent")  # has items, shipment, events
BLAIR_REPLACEMENT = ids.order_id("replacement_in_transit")
BLAIR_SHIPMENT = ids.shipment_id("lost_shipment_with_replacement_sent")
BLAIR_SUB = ids.subscription_id("annual_v1_inside_proration_window")


def test_every_function_returns_the_owners_data_to_the_owner(seeded: Connection) -> None:
    """The control: the same ids work for Blair, so the empty results below are the filter."""
    assert repo.get_order(seeded, BLAIR, "SIM-2001") is not None
    assert len(repo.items_of(seeded, BLAIR, BLAIR_ORDER)) == 2
    assert repo.shipment_of(seeded, BLAIR, BLAIR_ORDER) is not None
    assert len(repo.shipment_events(seeded, BLAIR, BLAIR_SHIPMENT)) == 3
    assert repo.replacements_of(seeded, BLAIR, BLAIR_ORDER) == ["SIM-2002"]
    assert repo.get_subscription(seeded, BLAIR, BLAIR_SUB) is not None
    assert {r.order_number for r in repo.list_orders(seeded, BLAIR)} >= {"SIM-2001", "SIM-2002"}
    assert len(repo.list_subscriptions(seeded, BLAIR)) == 2


def test_no_function_returns_another_customers_data(seeded: Connection) -> None:
    assert repo.get_order(seeded, ALEX, "SIM-2001") is None
    assert repo.items_of(seeded, ALEX, BLAIR_ORDER) == []
    assert repo.shipment_of(seeded, ALEX, BLAIR_ORDER) is None
    assert repo.shipment_events(seeded, ALEX, BLAIR_SHIPMENT) == []
    assert repo.replacements_of(seeded, ALEX, BLAIR_ORDER) == []
    assert repo.get_subscription(seeded, ALEX, BLAIR_SUB) is None
    listed = {r.order_number for r in repo.list_orders(seeded, ALEX)}
    assert listed and not listed & {"SIM-2001", "SIM-2002", "SIM-2003"}
    assert BLAIR_SUB not in {r.id for r in repo.list_subscriptions(seeded, ALEX)}


def test_the_replacement_join_cannot_expose_another_customers_order_number(
    seeded: Connection,
) -> None:
    """Even if a cross-customer link existed in the data (the composite FK forbids it), the join
    is constrained to the same customer, so the number would not leak."""
    row = repo.get_order(seeded, BLAIR, "SIM-2002")
    assert row is not None and row.replacement_for_number == "SIM-2001"
    count = seeded.execute(
        text("SELECT count(*) FROM orders WHERE customer_id = :c"), {"c": ALEX.customer_id}
    ).scalar_one()
    assert count == len(repo.list_orders(seeded, ALEX))
