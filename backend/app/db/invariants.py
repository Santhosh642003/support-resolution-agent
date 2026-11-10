"""Database invariant checks that run in CI and after each evaluation suite run."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Connection, text


@dataclass(frozen=True)
class RefundDrift:
    order_id: UUID
    recorded_cents: int
    summed_cents: int


@dataclass(frozen=True)
class RefundLineDrift:
    level: str  # 'item_total' (item vs its lines) or 'order_vs_lines' (order total vs all lines)
    entity_id: UUID
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


def find_refund_line_drift(conn: Connection) -> list[RefundLineDrift]:
    """Line-level drift: item running totals vs refund_lines, and order total vs all lines."""
    rows = conn.execute(
        text(
            "SELECT level, entity_id, recorded_cents, summed_cents FROM v_refund_line_drift"
            " ORDER BY level, entity_id"
        )
    )
    return [RefundLineDrift(r.level, r.entity_id, r.recorded_cents, r.summed_cents) for r in rows]


def assert_no_refund_drift(conn: Connection) -> None:
    """Both levels must agree with their records."""
    drift = find_refund_drift(conn)
    line_drift = find_refund_line_drift(conn)
    if drift or line_drift:
        raise RefundDriftError(
            f"refund totals drifted from records: order-level={drift} line-level={line_drift}"
        )
