"""Database invariant checks that run in CI and after each evaluation suite run."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Connection, text


@dataclass(frozen=True)
class RefundDrift:
    order_id: UUID
    recorded_cents: int
    summed_cents: int


class RefundDriftError(AssertionError):
    pass


def find_refund_drift(conn: Connection) -> list[RefundDrift]:
    """Orders whose denormalized refunded_total_cents differs from SUM(refunds.amount_cents)."""
    rows = conn.execute(
        text("SELECT order_id, recorded_cents, summed_cents FROM v_refund_drift ORDER BY order_id")
    )
    return [RefundDrift(r.order_id, r.recorded_cents, r.summed_cents) for r in rows]


def assert_no_refund_drift(conn: Connection) -> None:
    drift = find_refund_drift(conn)
    if drift:
        raise RefundDriftError(f"refunded_total_cents drifted from refund records: {drift}")
