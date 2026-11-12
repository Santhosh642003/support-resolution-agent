"""Read-only state APIs against real PostgreSQL with the real seed data.

Groundwork for adversarial family 1 (cross-customer access): every endpoint is attacked with
another customer's identifiers, and the response must be byte-identical to the one for an
identifier that does not exist. Identity comes only from the session cookie.
"""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from app.api.deps import get_conn
from app.auth.sessions import SESSION_COOKIE, create_session
from app.clock import SIMULATED_TODAY, FixedClock
from app.main import create_app
from app.seed import ids
from app.seed.loader import seed_all
from tests.db.helpers import db_error

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

ALEX_ORDERS = {"SIM-1001", "SIM-1002", "SIM-1003", "SIM-1004", "SIM-1005", "SIM-S-0003"}
BLAIR_ORDERS = {"SIM-2001", "SIM-2002", "SIM-2003", "SIM-S-0001", "SIM-S-0005"}


class World:
    def __init__(self, conn: Connection, engine: Engine) -> None:
        seed_all(conn)
        self.conn = conn
        self.engine = engine
        self.sessions = {
            name: create_session(conn, ids.customer_id(name)) for name in ("alex", "blair", "carol")
        }

    def build(self, clock: FixedClock | None = None) -> FastAPI:
        app = create_app(engine=self.engine, clock=clock or FixedClock())
        conn = self.conn

        def same_transaction() -> Iterator[Connection]:
            yield conn  # the test's own (uncommitted) transaction, which holds the seed

        app.dependency_overrides[get_conn] = same_transaction
        return app

    def client(self, who: str | None, clock: FixedClock | None = None) -> TestClient:
        c = TestClient(self.build(clock))
        if who is not None:
            c.cookies.set(SESSION_COOKIE, str(self.sessions[who]))
        return c


@pytest.fixture
def world(conn: Connection, engine: Engine) -> World:
    return World(conn, engine)


SUB_CASE8 = str(ids.subscription_id("annual_v2_day10_cooling_off_beats_proration"))  # alex's
SUB_CASE6 = str(ids.subscription_id("annual_v1_inside_proration_window"))  # blair's
SUB_CASE7 = str(ids.subscription_id("monthly_v2_inside_cooling_off"))  # carol's

ENDPOINTS = [
    "/orders",
    "/orders/SIM-1001",
    "/orders/SIM-1001/items",
    "/orders/SIM-2001/shipment",
    "/subscriptions",
    f"/subscriptions/{SUB_CASE8}",
]


# --- authentication: identity comes only from a live server-side session ---------------------


@pytest.mark.parametrize("url", ENDPOINTS)
def test_no_session_is_401_on_every_endpoint(world: World, url: str) -> None:
    r = world.client(None).get(url)
    assert r.status_code == 401 and r.json() == {"detail": "authentication required"}


def test_unknown_malformed_expired_and_revoked_sessions_are_the_same_401(world: World) -> None:
    conn = world.conn
    alex = ids.customer_id("alex")
    expired = conn.execute(
        text(
            "INSERT INTO auth_sessions (customer_id, created_at, expires_at)"
            " VALUES (:c, now() - interval '2 hours', now() - interval '1 hour') RETURNING id"
        ),
        {"c": alex},
    ).scalar_one()
    revoked = create_session(conn, alex)
    conn.execute(text("UPDATE auth_sessions SET revoked_at = now() WHERE id = :i"), {"i": revoked})
    bodies = []
    for cookie in (str(uuid4()), "not-a-uuid", "", str(expired), str(revoked)):
        c = world.client(None)
        c.cookies.set(SESSION_COOKIE, cookie)
        r = c.get("/orders")
        bodies.append((r.status_code, r.json()))
    assert set(map(str, bodies)) == {str((401, {"detail": "authentication required"}))}


def test_a_valid_session_works_and_the_customer_comes_from_it(world: World) -> None:
    r = world.client("alex").get("/orders")
    assert r.status_code == 200
    assert {o["order_number"] for o in r.json()["orders"]} == ALEX_ORDERS
    r = world.client("blair").get("/orders")
    assert {o["order_number"] for o in r.json()["orders"]} == BLAIR_ORDERS


# --- cross-customer: another customer's record is the SAME 404 as a nonexistent one -----------

FOREIGN_VS_MISSING = [
    ("/orders/{o}", "SIM-2001", "SIM-9999"),
    ("/orders/{o}/items", "SIM-2001", "SIM-9999"),
    ("/orders/{o}/shipment", "SIM-2001", "SIM-9999"),
]


@pytest.mark.parametrize(("template", "foreign", "missing"), FOREIGN_VS_MISSING)
def test_order_endpoints_return_an_identical_404_for_foreign_and_nonexistent_orders(
    world: World, template: str, foreign: str, missing: str
) -> None:
    alex = world.client("alex")
    a = alex.get(template.format(o=foreign))
    b = alex.get(template.format(o=missing))
    assert a.status_code == b.status_code == 404
    assert a.json() == b.json() == {"detail": "not found"}
    assert a.content == b.content  # byte-identical, not merely equal JSON
    assert a.headers["content-length"] == b.headers["content-length"]


def test_subscription_endpoint_returns_an_identical_404_for_foreign_missing_and_malformed_ids(
    world: World,
) -> None:
    alex = world.client("alex")
    responses = [
        alex.get(f"/subscriptions/{SUB_CASE6}"),  # blair's real subscription
        alex.get(f"/subscriptions/{SUB_CASE7}"),  # carol's real subscription
        alex.get(f"/subscriptions/{uuid4()}"),  # well-formed, nonexistent
        alex.get("/subscriptions/not-a-uuid"),  # malformed: no format oracle either
    ]
    assert {r.status_code for r in responses} == {404}
    assert len({r.content for r in responses}) == 1


def test_every_endpoint_is_attacked_from_every_direction_and_leaks_nothing(
    world: World,
) -> None:
    """Each customer asks for each other customer's records on every endpoint."""
    orders = {"alex": "SIM-1001", "blair": "SIM-2001", "carol": "SIM-3001"}
    subs = {"alex": SUB_CASE8, "blair": SUB_CASE6, "carol": SUB_CASE7}
    for attacker in orders:
        client = world.client(attacker)
        for victim in orders:
            if victim == attacker:
                continue
            for path in (
                f"/orders/{orders[victim]}",
                f"/orders/{orders[victim]}/items",
                f"/orders/{orders[victim]}/shipment",
                f"/subscriptions/{subs[victim]}",
            ):
                r = client.get(path)
                assert (r.status_code, r.json()) == (404, {"detail": "not found"}), (
                    attacker,
                    path,
                )
        # and the list endpoints contain only the attacker's own records
        listed = {o["order_number"] for o in client.get("/orders").json()["orders"]}
        assert orders[attacker] in listed
        assert not listed & {orders[v] for v in orders if v != attacker}
        subs_listed = {
            x["subscription_id"] for x in client.get("/subscriptions").json()["subscriptions"]
        }
        assert subs[attacker] in subs_listed
        assert not subs_listed & {subs[v] for v in subs if v != attacker}


def test_an_attacker_supplied_customer_id_is_ignored_everywhere(world: World) -> None:
    blair = str(ids.customer_id("blair"))
    alex = world.client("alex")
    listed = alex.get("/orders", params={"customer_id": blair}).json()["orders"]
    assert {o["order_number"] for o in listed} == ALEX_ORDERS
    assert alex.get("/orders/SIM-2001", params={"customer_id": blair}).status_code == 404
    assert alex.get(f"/subscriptions/{SUB_CASE6}", params={"customer_id": blair}).status_code == 404
    # nor via headers
    r = alex.get("/orders/SIM-2001", headers={"X-Customer-Id": blair, "customer_id": blair})
    assert r.status_code == 404


def test_a_replacement_link_never_points_at_another_customers_order(world: World) -> None:
    body = world.client("blair").get("/orders/SIM-2002").json()
    assert body["replacement_for_order_number"] == "SIM-2001"
    assert world.client("alex").get("/orders/SIM-2002").status_code == 404


# --- there is no customer id anywhere in the API surface ---------------------------------------


def _keys(node: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(node, dict):
        for k, v in node.items():
            out.add(str(k))
            out |= _keys(v)
    elif isinstance(node, list):
        for v in node:
            out |= _keys(v)
    return out


def test_no_route_parameter_request_body_or_response_field_names_a_customer(world: World) -> None:
    schema = world.build().openapi()
    for path, ops in schema["paths"].items():
        for method, op in ops.items():
            params = [p["name"].lower() for p in op.get("parameters", [])]
            assert not any("customer" in p for p in params), (path, method, params)
            if path != "/chat":  # the one write endpoint has its own walk (test_chat_api.py)
                assert "requestBody" not in op, (path, method)  # read-only: no bodies at all
                assert method == "get", (path, method)
    names = {k.lower() for k in _keys(schema["components"]["schemas"])}
    assert not any("customer" in n for n in names), sorted(n for n in names if "customer" in n)


def test_responses_never_contain_a_customer_or_internal_id(world: World) -> None:
    for url in ("/orders", "/orders/SIM-1001", "/orders/SIM-1001/shipment", "/subscriptions"):
        keys = {k.lower() for k in _keys(world.client("alex").get(url).json())}
        assert not any("customer" in k for k in keys), (url, keys)
    raw = world.client("alex").get("/orders/SIM-1001").text
    assert str(ids.customer_id("alex")) not in raw


def test_every_state_route_is_get_only_and_returns_a_named_typed_schema(world: World) -> None:
    paths = {
        p: ops
        for p, ops in world.build().openapi()["paths"].items()
        if p.startswith(("/orders", "/subscriptions"))
    }
    assert len(paths) == 6
    for path, ops in paths.items():
        assert set(ops) == {"get"}, path
        ok = ops["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        assert "$ref" in ok, (path, ok)  # a named response schema, not a free-form object


# --- the database refuses writes on the request connection -------------------------------------


def test_the_request_connection_is_read_only_at_the_database(engine: Engine) -> None:
    request = SimpleNamespace(method="GET")  # a GET: the read-only transaction
    with contextmanager(get_conn)(request, engine) as c:  # type: ignore[arg-type]
        with pytest.raises(DBAPIError) as exc:
            c.execute(
                text(
                    "INSERT INTO customers (email, display_name, password_hash)"
                    " VALUES ('x@simulated.example', 'x', 'x')"
                )
            )
        assert db_error(exc.value)[0] == "25006"  # read_only_sql_transaction


# --- content: business dates derived through the injected clock ---------------------------------


def test_order_detail_for_the_refund_ceiling_case(world: World) -> None:
    r = world.client("alex").get("/orders/SIM-1001")
    assert r.status_code == 200
    b = r.json()
    assert b["as_of"] == "2026-04-15" and b["simulated"] is True
    assert b["placed_on"] == "2026-02-26" and b["status"] == "delivered"
    assert (b["list_total_cents"], b["paid_cents"], b["refundable_remaining_cents"]) == (
        9800,
        9800,
        9800,
    )
    assert [i["sku"] for i in b["items"]] == ["SIM-BOX-DELUXE", "SIM-BOX-FAMILY"]
    s = b["shipment"]
    assert (s["shipped_on"], s["delivered_on"]) == ("2026-03-13", "2026-03-18")
    assert s["is_presumed_lost"] is False and [e["code"] for e in s["events"]][-1] == "DELIVERED"


def test_order_items_expose_paid_amount_flags_and_accessed_date(world: World) -> None:
    promo = world.client("alex").get("/orders/SIM-1002/items").json()["items"]
    assert [(i["unit_price_cents"], i["paid_cents"]) for i in promo] == [(3000, 2250), (5000, 3750)]
    digital = world.client("alex").get("/orders/SIM-1003/items").json()["items"]
    addon = next(i for i in digital if i["is_digital_addon"])
    assert addon["digital_accessed_on"] == "2026-03-30"
    final = world.client("alex").get("/orders/SIM-1004/items").json()["items"]
    assert [i["final_sale"] for i in final] == [False, True]


def test_a_presumed_lost_shipment_and_its_replacement(world: World) -> None:
    blair = world.client("blair")
    original = blair.get("/orders/SIM-2001").json()
    s = original["shipment"]
    assert s["shipped_on"] == "2026-03-11" and s["delivered_on"] is None
    assert s["policy_expected_on"] == "2026-03-20" and s["presumed_lost_on"] == "2026-03-25"
    assert s["is_presumed_lost"] is True and s["days_since_shipped"] == 35
    assert original["replaced_by_order_numbers"] == ["SIM-2002"]
    rep = blair.get("/orders/SIM-2002/shipment").json()
    assert rep["presumed_lost_on"] == "2026-04-20" and rep["is_presumed_lost"] is False
    assert blair.get("/orders/SIM-2002").json()["paid_cents"] == 0


def test_a_delivery_exception_is_visible_before_the_presumed_lost_date(world: World) -> None:
    s = world.client("carol").get("/orders/SIM-3001/shipment").json()
    assert s["has_delivery_exception"] is True and s["is_presumed_lost"] is False
    assert s["presumed_lost_on"] == "2026-04-16"


def test_subscription_state_and_days_since_first_charge(world: World) -> None:
    s = world.client("alex").get(f"/subscriptions/{SUB_CASE8}").json()
    assert s["plan_type"] == "annual" and s["status"] == "active"
    assert (s["first_charge_on"], s["days_since_first_charge"]) == ("2026-04-05", 10)
    assert s["current_period_end_on"] == "2027-04-05" and s["canceled_on"] is None
    carol = world.client("carol").get(f"/subscriptions/{SUB_CASE7}").json()
    assert carol["days_since_first_charge"] == 7 and carol["plan_type"] == "monthly"
    assert len(world.client("carol").get("/subscriptions").json()["subscriptions"]) == 2


def test_derived_facts_move_with_the_injected_clock_and_nothing_else(world: World) -> None:
    def shipment(clock: FixedClock) -> dict[str, Any]:
        return world.client("blair", clock).get("/orders/SIM-2002/shipment").json()  # type: ignore[no-any-return]

    base = FixedClock()
    assert base.today() == SIMULATED_TODAY
    four_later = shipment(base.plus_days(4))  # 2026-04-19: the day before presumed lost
    five_later = shipment(base.plus_days(5))  # 2026-04-20: presumed lost
    assert (four_later["is_presumed_lost"], five_later["is_presumed_lost"]) == (False, True)
    assert (four_later["as_of"], five_later["as_of"]) == ("2026-04-19", "2026-04-20")
    assert four_later["presumed_lost_on"] == five_later["presumed_lost_on"] == "2026-04-20"
    sub = world.client("alex", base.plus_days(10)).get(f"/subscriptions/{SUB_CASE8}").json()
    assert sub["days_since_first_charge"] == 20  # past the 14-day cooling-off window


def test_every_date_in_every_response_is_a_plain_business_date(world: World) -> None:
    def dates(node: Any, key: str = "") -> list[tuple[str, Any]]:
        out: list[tuple[str, Any]] = []
        if isinstance(node, dict):
            for k, v in node.items():
                out += dates(v, k)
        elif isinstance(node, list):
            for v in node:
                out += dates(v, key)
        elif key.endswith("_on") or key == "as_of":
            out.append((key, node))
        return out

    seen = 0
    for url in ("/orders", "/orders/SIM-1001", "/orders/SIM-2001/shipment", "/subscriptions"):
        for key, value in dates(
            world.client(
                "alex" if "1001" in url or url in ("/orders", "/subscriptions") else "blair"
            )
            .get(url)
            .json()
        ):
            if value is not None:
                assert ISO_DATE.match(value), (url, key, value)  # a date, never a timestamp
                seen += 1
    assert seen > 15


def test_the_running_app_defaults_to_the_simulated_clock() -> None:
    assert create_app().state.clock.today() == date(2026, 4, 15)
