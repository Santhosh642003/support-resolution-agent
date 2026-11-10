"""Adversarial family 5: a GENUINE race. Two separate database connections, driven by two
threads, attempt the same confirmed refund at the same moment. Nothing here is simulated
sequentially."""

import threading
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db.invariants import assert_no_refund_drift
from tests.db.helpers import db_error

PAID = 10000
AMOUNT = 10000  # each racer tries to refund the full paid amount


@dataclass
class Outcome:
    result: str = "unset"
    detail: str = ""


def _seed(engine: Engine) -> tuple[UUID, UUID, UUID]:
    """Committed rows (the racers run on other connections). Returns (customer, order, item)."""
    with engine.begin() as c:
        cust = c.execute(
            text(
                "INSERT INTO customers (email, display_name, password_hash)"
                " VALUES (:e, 'Race', 'x') RETURNING id"
            ),
            {"e": f"{uuid4()}@example.test"},
        ).scalar_one()
        order = c.execute(
            text(
                "INSERT INTO orders (order_number, customer_id, placed_at, list_total_cents,"
                " paid_cents) VALUES (:n, :c, now(), :p, :p) RETURNING id"
            ),
            {"n": f"SIM-{uuid4().hex[:10]}", "c": cust, "p": PAID},
        ).scalar_one()
        product = c.execute(
            text(
                "INSERT INTO products (sku, name, price_cents) VALUES (:s, 'x', 100) RETURNING id"
            ),
            {"s": uuid4().hex},
        ).scalar_one()
        item = c.execute(
            text(
                "INSERT INTO order_items (order_id, customer_id, product_id, qty,"
                " unit_price_cents, paid_cents) VALUES (:o, :c, :p, 1, :a, :a) RETURNING id"
            ),
            {"o": order, "c": cust, "p": product, "a": PAID},
        ).scalar_one()
    return cust, order, item


def _prepare_decision_and_action(engine: Engine, cust: UUID, order: UUID) -> tuple[UUID, str]:
    """One decision per racer (an action has exactly one decision); returns (action_id, key)."""
    with engine.begin() as c:
        conv = c.execute(
            text("INSERT INTO conversations (customer_id) VALUES (:c) RETURNING id"), {"c": cust}
        ).scalar_one()
        dec = c.execute(
            text(
                "INSERT INTO decisions (conversation_id, customer_id, action_type, order_id,"
                " outcome, amount_cents, reason_codes, rule_set_version, input_state)"
                " VALUES (:v, :c, 'refund', :o, 'allowed', :a, ARRAY['T'], 't', '{}'::jsonb)"
                " RETURNING id"
            ),
            {"v": conv, "c": cust, "o": order, "a": AMOUNT},
        ).scalar_one()
        key = uuid4().hex
        act = c.execute(
            text(
                "INSERT INTO actions (decision_id, customer_id, action_type, idempotency_key,"
                " amount_cents) VALUES (:d, :c, 'refund', :k, :a) RETURNING id"
            ),
            {"d": dec, "c": cust, "k": key, "a": AMOUNT},
        ).scalar_one()
    return act, key


def _race(engine: Engine, use_row_lock: bool) -> list[Outcome]:
    cust, order, item = _seed(engine)
    actions = [_prepare_decision_and_action(engine, cust, order) for _ in range(2)]
    barrier = threading.Barrier(2)
    outcomes = [Outcome(), Outcome()]

    def racer(i: int) -> None:
        action_id, _ = actions[i]
        try:
            with engine.connect() as c, c.begin():
                barrier.wait(timeout=10)
                if use_row_lock:
                    row = c.execute(
                        text(
                            "SELECT paid_cents, refunded_total_cents FROM orders"
                            " WHERE id = :o FOR UPDATE"
                        ),
                        {"o": order},
                    ).one()
                    # Hold the lock while "computing", so the other racer really contends.
                    c.execute(text("SELECT pg_sleep(0.3)"))
                    if row.paid_cents - row.refunded_total_cents < AMOUNT:
                        outcomes[i] = Outcome("declined", "rules re-run: nothing left to refund")
                        return
                else:
                    c.execute(text("SELECT pg_sleep(0.3)"))  # naive: no lock, no re-check
                refund_id = c.execute(
                    text(
                        "INSERT INTO refunds (action_id, order_id, customer_id, amount_cents)"
                        " VALUES (:a, :o, :c, :amt) RETURNING id"
                    ),
                    {"a": action_id, "o": order, "c": cust, "amt": AMOUNT},
                ).scalar_one()
                c.execute(
                    text(
                        "INSERT INTO refund_lines (refund_id, order_item_id, order_id,"
                        " customer_id, amount_cents) VALUES (:r, :i, :o, :c, :amt)"
                    ),
                    {"r": refund_id, "i": item, "o": order, "c": cust, "amt": AMOUNT},
                )
            outcomes[i] = Outcome("refunded")
        except (IntegrityError, DBAPIError) as exc:
            state, constraint = db_error(exc)
            outcomes[i] = Outcome("rejected_by_db", f"{state} {constraint}")

    threads = [threading.Thread(target=racer, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "racer deadlocked or hung"

    with engine.connect() as c:
        total = c.execute(
            text("SELECT refunded_total_cents FROM orders WHERE id = :o"), {"o": order}
        ).scalar_one()
        n = c.execute(
            text("SELECT count(*) FROM refunds WHERE order_id = :o"), {"o": order}
        ).scalar_one()
        assert_no_refund_drift(c)
    assert total == PAID and n == 1, f"expected exactly one full refund, got total={total} rows={n}"
    return outcomes


def test_race_with_row_lock_exactly_one_refund_and_loser_declines_cleanly(engine: Engine) -> None:
    outcomes = _race(engine, use_row_lock=True)
    assert sorted(o.result for o in outcomes) == ["declined", "refunded"]


def test_race_without_row_lock_database_still_prevents_double_refund(engine: Engine) -> None:
    """Defense in depth: even a buggy caller that skips the lock cannot over-refund."""
    outcomes = _race(engine, use_row_lock=False)
    assert sorted(o.result for o in outcomes) == ["refunded", "rejected_by_db"]
    loser = next(o for o in outcomes if o.result == "rejected_by_db")
    assert "orders_refund_within_paid" in loser.detail
