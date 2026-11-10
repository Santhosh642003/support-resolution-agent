"""Tiny SQL builders for tests. All data is synthetic."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError


def one(conn: Connection, sql: str, **params: Any) -> Any:
    return conn.execute(text(sql), params).scalar_one()


def one_uuid(conn: Connection, sql: str, **params: Any) -> UUID:
    value = one(conn, sql, **params)
    assert isinstance(value, UUID)
    return value


def make_customer(conn: Connection) -> UUID:
    return one_uuid(
        conn,
        "INSERT INTO customers (email, display_name, password_hash)"
        " VALUES (:e, 'Test Customer', 'x') RETURNING id",
        e=f"{uuid4()}@example.test",
    )


def make_product(conn: Connection) -> UUID:
    return one_uuid(
        conn,
        "INSERT INTO products (sku, name, price_cents) VALUES (:s, 'Simulated item', 100)"
        " RETURNING id",
        s=uuid4().hex,
    )


def make_item(
    conn: Connection, customer_id: UUID, order_id: UUID, paid: int, *, final_sale: bool = False
) -> UUID:
    return one_uuid(
        conn,
        "INSERT INTO order_items (order_id, customer_id, product_id, qty, unit_price_cents,"
        " paid_cents, final_sale) VALUES (:o, :c, :p, 1, :paid, :paid, :fs) RETURNING id",
        o=order_id,
        c=customer_id,
        p=make_product(conn),
        paid=paid,
        fs=final_sale,
    )


def make_order(
    conn: Connection,
    customer_id: UUID,
    *,
    list_total: int = 10000,
    discount: int = 0,
    shipping: int = 0,
    placed_at: datetime | None = None,
    items: list[int] | None = None,
) -> UUID:
    """Create an order. By default it has ONE item paid for list_total - discount; pass
    `items=[...]` (paid amounts) for several. Shipping is not part of any item."""
    order_id = one_uuid(
        conn,
        "INSERT INTO orders (order_number, customer_id, placed_at, list_total_cents,"
        " discount_cents, shipping_cents, paid_cents)"
        " VALUES (:n, :c, :p, :l, :d, :s, :paid) RETURNING id",
        n=f"SIM-{uuid4().hex[:10]}",
        c=customer_id,
        p=placed_at or datetime(2026, 2, 20, tzinfo=UTC),
        l=list_total,
        d=discount,
        s=shipping,
        paid=list_total - discount + shipping,
    )
    for paid in items if items is not None else [list_total - discount]:
        make_item(conn, customer_id, order_id, paid)
    return order_id


def item_ids(conn: Connection, order_id: UUID) -> list[UUID]:
    rows = conn.execute(
        text("SELECT id FROM order_items WHERE order_id = :o ORDER BY paid_cents DESC, id"),
        {"o": order_id},
    )
    return [r.id for r in rows]


def make_conversation(conn: Connection, customer_id: UUID) -> UUID:
    return one_uuid(
        conn,
        "INSERT INTO conversations (customer_id) VALUES (:c) RETURNING id",
        c=customer_id,
    )


def make_decision(
    conn: Connection,
    customer_id: UUID,
    order_id: UUID,
    *,
    outcome: str = "allowed",
    amount: int = 5000,
) -> UUID:
    conv = make_conversation(conn, customer_id)
    return one_uuid(
        conn,
        "INSERT INTO decisions (conversation_id, customer_id, action_type, order_id, outcome,"
        " amount_cents, reason_codes, rule_set_version, input_state)"
        " VALUES (:conv, :c, 'refund', :o, :out, :amt, ARRAY['TEST'], 'test-1', '{}'::jsonb)"
        " RETURNING id",
        conv=conv,
        c=customer_id,
        o=order_id,
        out=outcome,
        amt=amount,
    )


def make_action(
    conn: Connection,
    customer_id: UUID,
    decision_id: UUID,
    *,
    amount: int = 5000,
    key: str | None = None,
) -> UUID:
    return one_uuid(
        conn,
        "INSERT INTO actions (decision_id, customer_id, action_type, idempotency_key, amount_cents)"
        " VALUES (:d, :c, 'refund', :k, :amt) RETURNING id",
        d=decision_id,
        c=customer_id,
        k=key or uuid4().hex,
        amt=amount,
    )


def make_refund(
    conn: Connection,
    customer_id: UUID,
    order_id: UUID,
    action_id: UUID,
    amount: int,
    *,
    lines: list[tuple[UUID, int]] | None = None,
) -> UUID:
    """Insert a refund header and its lines. Default: one line on the order's first item."""
    refund_id = one_uuid(
        conn,
        "INSERT INTO refunds (action_id, order_id, customer_id, amount_cents)"
        " VALUES (:a, :o, :c, :amt) RETURNING id",
        a=action_id,
        o=order_id,
        c=customer_id,
        amt=amount,
    )
    for item_id, line_amount in (
        lines if lines is not None else [(item_ids(conn, order_id)[0], amount)]
    ):
        conn.execute(
            text(
                "INSERT INTO refund_lines (refund_id, order_item_id, order_id, customer_id,"
                " amount_cents) VALUES (:r, :i, :o, :c, :amt)"
            ),
            {"r": refund_id, "i": item_id, "o": order_id, "c": customer_id, "amt": line_amount},
        )
    return refund_id


def make_token(
    conn: Connection,
    customer_id: UUID,
    decision_id: UUID,
    *,
    ttl: timedelta = timedelta(minutes=15),
) -> str:
    """Insert a token and return the RAW token (only its hash is stored)."""
    raw = secrets.token_urlsafe(32)
    conn.execute(
        text(
            "INSERT INTO confirmation_tokens (token_hash, customer_id, decision_id, created_at,"
            " expires_at) VALUES (:h, :c, :d, now() - interval '1 second', now() + :ttl)"
        ),
        {"h": hash_token(raw), "c": customer_id, "d": decision_id, "ttl": ttl},
    )
    return raw


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def expect_fail(conn: Connection, sql: str, **params: Any) -> tuple[str | None, str | None]:
    """Run a statement that must fail; return (SQLSTATE, constraint). Uses a savepoint so the
    outer transaction survives."""
    try:
        with conn.begin_nested():
            conn.execute(text(sql), params)
    except DBAPIError as exc:
        return db_error(exc)
    raise AssertionError(f"statement unexpectedly succeeded: {sql}")


def db_error(exc: DBAPIError) -> tuple[str | None, str | None]:
    """(SQLSTATE, constraint name) of a wrapped driver error."""
    orig = exc.orig
    diag = getattr(orig, "diag", None)
    return getattr(orig, "sqlstate", None), getattr(diag, "constraint_name", None)
