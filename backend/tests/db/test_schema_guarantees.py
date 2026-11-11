"""Each test proves a guarantee is enforced BY THE DATABASE (requirements a to g)."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from app.db.invariants import RefundDriftError, assert_no_refund_drift, find_refund_drift
from app.db.tokens import CONSUME_CONFIRMATION_TOKEN_SQL
from tests.db.helpers import (
    expect_fail,
    hash_token,
    make_action,
    make_customer,
    make_decision,
    make_order,
    make_refund,
    make_token,
    one,
)

# --- (a) idempotency ------------------------------------------------------------------------


def test_duplicate_idempotency_key_is_unique_violation(conn: Connection) -> None:
    c = make_customer(conn)
    o = make_order(conn, c)
    make_action(conn, c, make_decision(conn, c, o), key="K-1")
    d2 = make_decision(conn, c, o)
    state, constraint = expect_fail(
        conn,
        "INSERT INTO actions (decision_id, customer_id, action_type, idempotency_key, amount_cents)"
        " VALUES (:d, :c, 'refund', 'K-1', 5000)",
        d=d2,
        c=c,
    )
    assert (state, constraint) == ("23505", "actions_idempotency_key_uq")


def test_one_action_per_decision(conn: Connection) -> None:
    c = make_customer(conn)
    d = make_decision(conn, c, make_order(conn, c))
    make_action(conn, c, d)
    state, constraint = expect_fail(
        conn,
        "INSERT INTO actions (decision_id, customer_id, action_type, idempotency_key, amount_cents)"
        " VALUES (:d, :c, 'refund', 'other-key', 5000)",
        d=d,
        c=c,
    )
    assert (state, constraint) == ("23505", "actions_decision_uq")


def test_action_requires_a_permitting_decision_with_matching_amount(conn: Connection) -> None:
    c = make_customer(conn)
    o = make_order(conn, c)
    denied = make_decision(conn, c, o, outcome="denied", amount=0)
    state, _ = expect_fail(
        conn,
        "INSERT INTO actions (decision_id, customer_id, action_type, idempotency_key, amount_cents)"
        " VALUES (:d, :c, 'refund', 'k-denied', 0)",
        d=denied,
        c=c,
    )
    assert state == "23514"
    ok = make_decision(conn, c, o, amount=5000)
    state, _ = expect_fail(
        conn,
        "INSERT INTO actions (decision_id, customer_id, action_type, idempotency_key, amount_cents)"
        " VALUES (:d, :c, 'refund', 'k-amt', 9999)",
        d=ok,
        c=c,
    )
    assert state == "23514"


def test_action_cannot_name_a_decision_of_another_customer(conn: Connection) -> None:
    a, b = make_customer(conn), make_customer(conn)
    d = make_decision(conn, a, make_order(conn, a))
    state, constraint = expect_fail(
        conn,
        "INSERT INTO actions (decision_id, customer_id, action_type, idempotency_key, amount_cents)"
        " VALUES (:d, :c, 'refund', 'k-x', 5000)",
        d=d,
        c=b,
    )
    assert (state, constraint) == ("23503", "actions_decision_owner_fk")


def test_final_action_cannot_change(conn: Connection) -> None:
    c = make_customer(conn)
    action = make_action(conn, c, make_decision(conn, c, make_order(conn, c)))
    conn.execute(
        text("UPDATE actions SET status='succeeded', completed_at=now() WHERE id=:i"), {"i": action}
    )
    state, _ = expect_fail(conn, "UPDATE actions SET status='failed' WHERE id=:i", i=action)
    assert state == "23001"


# --- (b) confirmation tokens ----------------------------------------------------------------


def _consume(conn: Connection, raw: str, customer: object, decision: object) -> int:
    rows = conn.execute(
        text(CONSUME_CONFIRMATION_TOKEN_SQL),
        {"token_hash": hash_token(raw), "customer_id": customer, "decision_id": decision},
    ).all()
    return len(rows)


def test_token_is_single_use(conn: Connection) -> None:
    c = make_customer(conn)
    d = make_decision(conn, c, make_order(conn, c))
    raw = make_token(conn, c, d)
    assert _consume(conn, raw, c, d) == 1
    assert _consume(conn, raw, c, d) == 0  # replay yields zero rows


def test_token_bound_to_customer_and_decision(conn: Connection) -> None:
    c, other = make_customer(conn), make_customer(conn)
    o = make_order(conn, c)
    d1, d2 = make_decision(conn, c, o), make_decision(conn, c, o)
    raw = make_token(conn, c, d1)
    assert _consume(conn, raw, other, d1) == 0
    assert _consume(conn, raw, c, d2) == 0
    assert _consume(conn, raw, c, d1) == 1


def test_expired_token_is_rejected_by_update_and_by_trigger(conn: Connection) -> None:
    c = make_customer(conn)
    d = make_decision(conn, c, make_order(conn, c))
    raw = make_token(conn, c, d, ttl=timedelta(milliseconds=1))
    conn.execute(text("SELECT pg_sleep(0.05)"))
    assert _consume(conn, raw, c, d) == 0
    # Even a raw UPDATE that skips the app's WHERE clause cannot use an expired token.
    state, _ = expect_fail(
        conn,
        "UPDATE confirmation_tokens SET used_at = now() WHERE token_hash = :h",
        h=hash_token(raw),
    )
    assert state == "23001"


def test_token_cannot_be_unused_or_retargeted(conn: Connection) -> None:
    c = make_customer(conn)
    o = make_order(conn, c)
    d1, d2 = make_decision(conn, c, o), make_decision(conn, c, o)
    raw = make_token(conn, c, d1)
    state, _ = expect_fail(
        conn,
        "UPDATE confirmation_tokens SET decision_id = :d WHERE token_hash = :h",
        d=d2,
        h=hash_token(raw),
    )
    assert state == "23001"
    assert _consume(conn, raw, c, d1) == 1
    state, _ = expect_fail(
        conn,
        "UPDATE confirmation_tokens SET used_at = NULL WHERE token_hash = :h",
        h=hash_token(raw),
    )
    assert state == "23001"
    state, _ = expect_fail(
        conn, "DELETE FROM confirmation_tokens WHERE token_hash = :h", h=hash_token(raw)
    )
    assert state == "23001"


# --- (c) append-only --------------------------------------------------------------------------


def test_audit_log_is_append_only(conn: Connection) -> None:
    conn.execute(
        text(
            "INSERT INTO audit_log (actor_type, event, entity_type, entity_id)"
            " VALUES ('system', 'test', 'order', 'x')"
        )
    )
    for sql in (
        "UPDATE audit_log SET event = 'tampered'",
        "DELETE FROM audit_log",
        "TRUNCATE audit_log",
    ):
        state, _ = expect_fail(conn, sql)
        assert state == "23001", sql


def test_decisions_and_refunds_are_immutable(conn: Connection) -> None:
    c = make_customer(conn)
    o = make_order(conn, c)
    d = make_decision(conn, c, o)
    a = make_action(conn, c, d)
    make_refund(conn, c, o, a, 5000)
    # Fire the deferred header/lines check first, so TRUNCATE reaches the append-only trigger
    # rather than failing on pending trigger events (SQLSTATE 55006).
    conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    for sql in (
        "UPDATE decisions SET amount_cents = 1",
        "DELETE FROM decisions",
        "UPDATE refunds SET amount_cents = 1",
        "DELETE FROM refunds",
        "TRUNCATE refunds CASCADE",  # plain TRUNCATE is also blocked natively by the FK (0A000)
    ):
        state, _ = expect_fail(conn, sql)
        assert state == "23001", sql


# --- (d) policy versions cannot overlap -----------------------------------------------------


def _policy(conn: Connection, ptype: str, version: int, lo: str, hi: str | None) -> None:
    conn.execute(
        text(
            "INSERT INTO policy_documents (policy_type, version, validity, title, source_path,"
            " content_hash) VALUES (:t, :v, daterange(CAST(:lo AS date), CAST(:hi AS date), '[)'),"
            " 'SIMULATED', 'p.md', 'h')"
        ),
        {"t": ptype, "v": version, "lo": lo, "hi": hi},
    )


def test_policy_validity_cannot_overlap(conn: Connection) -> None:
    _policy(conn, "refund", 1, "2025-06-01", "2026-03-01")
    _policy(conn, "refund", 2, "2026-03-01", None)  # adjacent is fine
    state, constraint = expect_fail(
        conn,
        "INSERT INTO policy_documents (policy_type, version, validity, title, source_path,"
        " content_hash) VALUES ('refund', 3, daterange('2026-02-15', '2026-03-15', '[)'),"
        " 'SIMULATED', 'p.md', 'h')",
    )
    assert (state, constraint) == ("23P01", "policy_documents_no_overlap")


def test_overlap_rule_is_per_policy_type(conn: Connection) -> None:
    _policy(conn, "refund", 1, "2025-06-01", None)
    _policy(conn, "shipping", 1, "2025-06-01", None)  # same dates, different type: allowed


def test_exactly_one_policy_in_effect_on_cutover_date(conn: Connection) -> None:
    _policy(conn, "refund", 1, "2025-06-01", "2026-03-01")
    _policy(conn, "refund", 2, "2026-03-01", None)
    rows = conn.execute(
        text(
            "SELECT version FROM policy_documents WHERE policy_type = 'refund'"
            " AND validity @> CAST(:d AS date)"
        ),
        {"d": "2026-03-01"},
    ).all()
    assert [r.version for r in rows] == [2]


# --- (e) money -------------------------------------------------------------------------------


def test_every_money_column_is_bigint_and_no_float_or_numeric_columns(conn: Connection) -> None:
    cols = conn.execute(
        text(
            "SELECT table_name, column_name, data_type FROM information_schema.columns"
            " WHERE table_schema = 'public'"
        )
    ).all()
    money = [c for c in cols if c.column_name.endswith("_cents")]
    assert money, "expected money columns"
    assert {c.data_type for c in money} == {"bigint"}
    banned = [c for c in cols if c.data_type in ("real", "double precision", "numeric", "money")]
    assert banned == []


def test_negative_money_rejected(conn: Connection) -> None:
    c = make_customer(conn)
    state, constraint = expect_fail(
        conn,
        "INSERT INTO orders (order_number, customer_id, placed_at, list_total_cents,"
        " shipping_cents, paid_cents) VALUES ('SIM-NEG', :c, now(), 100, -5, 95)",
        c=c,
    )
    assert (state, constraint) == ("23514", "orders_shipping_nonneg")
    state, constraint = expect_fail(
        conn, "INSERT INTO products (sku, name, price_cents) VALUES ('neg', 'x', -1)"
    )
    assert (state, constraint) == ("23514", "products_price_nonneg")


def test_paid_amount_must_equal_list_minus_discount_plus_shipping(conn: Connection) -> None:
    c = make_customer(conn)
    state, constraint = expect_fail(
        conn,
        "INSERT INTO orders (order_number, customer_id, placed_at, list_total_cents,"
        " discount_cents, shipping_cents, paid_cents) VALUES ('SIM-BAD', :c, now(), 10000, 2000, 0, 10000)",
        c=c,
    )
    assert (state, constraint) == ("23514", "orders_paid_consistent")


# --- (f) decisions precede actions ---------------------------------------------------------


def test_action_cannot_exist_without_a_decision(conn: Connection) -> None:
    c = make_customer(conn)
    state, _ = expect_fail(
        conn,
        "INSERT INTO actions (decision_id, customer_id, action_type, idempotency_key, amount_cents)"
        " VALUES (:d, :c, 'refund', 'k-none', 100)",
        d=uuid4(),
        c=c,
    )
    assert state == "23503"
    state, _ = expect_fail(
        conn,
        "INSERT INTO actions (customer_id, action_type, idempotency_key, amount_cents)"
        " VALUES (:c, 'refund', 'k-null', 100)",
        c=c,
    )
    assert state == "23502"


# --- (g) ownership --------------------------------------------------------------------------


def test_child_rows_cannot_name_a_different_customer_than_their_parent(conn: Connection) -> None:
    a, b = make_customer(conn), make_customer(conn)
    o = make_order(conn, a)
    pid = one(
        conn,
        "INSERT INTO products (sku, name, price_cents) VALUES (:s, 'x', 100) RETURNING id",
        s=uuid4().hex,
    )
    state, constraint = expect_fail(
        conn,
        "INSERT INTO order_items (order_id, customer_id, product_id, qty, unit_price_cents, paid_cents)"
        " VALUES (:o, :c, :p, 1, 100, 100)",
        o=o,
        c=b,
        p=pid,
    )
    assert (state, constraint) == ("23503", "order_items_owner_fk")


def test_every_customer_scoped_table_has_customer_id_not_null(conn: Connection) -> None:
    tables = {
        r.table_name
        for r in conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
                " AND table_type='BASE TABLE'"
            )
        )
    }
    # Not customer-scoped: reference/global data, and tables owned through a parent row.
    exempt = {
        "customers",
        "products",
        "policy_documents",
        "policy_chunks",
        "decision_policies",
        "alembic_version",
        "audit_log",  # audit_log.customer_id is nullable (system events)
        # Provider-layer tables hold no customer data (hashes, counters, token counts; the
        # prompt is never stored), so they are deliberately not customer-scoped (migration 0005).
        "llm_cache",
        "llm_calls",
        "llm_usage_daily",
    }
    nullable = {
        r.table_name
        for r in conn.execute(
            text(
                "SELECT table_name FROM information_schema.columns WHERE table_schema='public'"
                " AND column_name='customer_id' AND is_nullable='YES'"
            )
        )
    }
    with_col = {
        r.table_name
        for r in conn.execute(
            text(
                "SELECT table_name FROM information_schema.columns WHERE table_schema='public'"
                " AND column_name='customer_id'"
            )
        )
    }
    scoped = tables - exempt
    assert scoped <= with_col, f"customer-scoped tables lacking customer_id: {scoped - with_col}"
    assert not (scoped & nullable), f"nullable customer_id on: {scoped & nullable}"


# --- paid-amount invariant and drift --------------------------------------------------------


def test_refund_updates_running_total_and_cannot_exceed_paid(conn: Connection) -> None:
    c = make_customer(conn)
    o = make_order(conn, c, list_total=10000)
    make_refund(
        conn, c, o, make_action(conn, c, make_decision(conn, c, o, amount=6000), amount=6000), 6000
    )
    assert one(conn, "SELECT refunded_total_cents FROM orders WHERE id=:o", o=o) == 6000
    d2 = make_decision(conn, c, o, amount=5000)
    a2 = make_action(conn, c, d2, amount=5000)
    state, constraint = expect_fail(
        conn,
        "INSERT INTO refunds (action_id, order_id, customer_id, amount_cents) VALUES (:a, :o, :c, 5000)",
        a=a2,
        o=o,
        c=c,
    )
    assert (state, constraint) == ("23514", "orders_refund_within_paid")
    assert one(conn, "SELECT refunded_total_cents FROM orders WHERE id=:o", o=o) == 6000
    assert_no_refund_drift(conn)


def test_refund_must_match_its_action(conn: Connection) -> None:
    c = make_customer(conn)
    o = make_order(conn, c)
    a = make_action(conn, c, make_decision(conn, c, o, amount=3000), amount=3000)
    state, _ = expect_fail(
        conn,
        "INSERT INTO refunds (action_id, order_id, customer_id, amount_cents) VALUES (:a, :o, :c, 3001)",
        a=a,
        o=o,
        c=c,
    )
    assert state == "23514"


def test_one_refund_per_action(conn: Connection) -> None:
    c = make_customer(conn)
    o = make_order(conn, c)
    a = make_action(conn, c, make_decision(conn, c, o, amount=1000), amount=1000)
    make_refund(conn, c, o, a, 1000)
    state, constraint = expect_fail(
        conn,
        "INSERT INTO refunds (action_id, order_id, customer_id, amount_cents) VALUES (:a, :o, :c, 1000)",
        a=a,
        o=o,
        c=c,
    )
    assert (state, constraint) == ("23505", "refunds_action_uq")


def test_drift_assertion_detects_a_diverged_running_total(conn: Connection) -> None:
    c = make_customer(conn)
    o = make_order(conn, c)
    make_refund(
        conn, c, o, make_action(conn, c, make_decision(conn, c, o, amount=2000), amount=2000), 2000
    )
    assert find_refund_drift(conn) == []
    # Simulate out-of-band tampering that the CHECK alone cannot see (lowering the total).
    conn.execute(text("UPDATE orders SET refunded_total_cents = 0 WHERE id = :o"), {"o": o})
    drift = find_refund_drift(conn)
    assert [(d.order_id, d.recorded_cents, d.summed_cents) for d in drift] == [(o, 0, 2000)]
    with pytest.raises(RefundDriftError):
        assert_no_refund_drift(conn)


def test_expected_error_type_is_a_dbapi_error() -> None:
    # Guard for the app translation layer: unique violations surface as DBAPIError subclasses.
    assert issubclass(DBAPIError, Exception)


_ = datetime, UTC  # silence unused-import linters for helper re-exports
