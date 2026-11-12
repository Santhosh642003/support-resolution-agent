"""The determination record: the SAME SHAPE as a Decision, authorising no action.

An order-status determination carries the clause keys it applied, the input state it was computed
from, reason codes and a rule-set version, exactly like the refund and cancellation Decisions of M2
will. It is stored in the `decisions` table (action_type 'order_status', outcome 'informational'),
so M2 extends one record shape instead of adding a parallel one. A database trigger already refuses
an action that references anything but an 'allowed' decision, so a determination can never be used
to execute anything (tested).

`decision_clauses` holds the applied clause keys; THOSE are the citations (SPEC 10.4).
"""

from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from app.auth.context import CustomerContext
from app.domain.rules.order_status import (
    RULE_SET_VERSION,
    OrderStatusInput,
    OrderStatusResult,
)


@dataclass(frozen=True)
class Determination:
    order_id: UUID
    order_number: str
    result: OrderStatusResult
    input_state: dict[str, Any]
    rule_set_version: str = RULE_SET_VERSION
    kind: str = "order_status"
    authorizes_action: bool = False  # a status determination never authorises anything

    @property
    def outcome(self) -> str:
        return self.result.outcome.value

    @property
    def reason_codes(self) -> tuple[str, ...]:
        return tuple(r.value for r in self.result.reason_codes)

    @property
    def applied_clause_keys(self) -> tuple[str, ...]:
        return self.result.applied_clause_keys

    @property
    def decision_outcome(self) -> str:
        return self.result.decision_outcome

    def allowed_dates(self) -> set[str]:
        """Every ISO date the explanation may mention: exactly those in the determination."""
        return {v for v in self.result.facts.values() if isinstance(v, str) and _is_iso(v)}

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "order_number": self.order_number,
            "outcome": self.outcome,
            "decision_outcome": self.decision_outcome,
            "reason_codes": list(self.reason_codes),
            "applied_clause_keys": list(self.applied_clause_keys),
            "policy_versions": {"shipping": self.result.policy_version},
            "rule_set_version": self.rule_set_version,
            "authorizes_action": self.authorizes_action,
            "facts": dict(self.result.facts),
            "input_state": self.input_state,
        }


def _is_iso(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return len(value) == 10


def input_state_of(inp: OrderStatusInput) -> dict[str, Any]:
    """JSON-able snapshot of exactly what the rules were given."""
    ship = inp.shipment
    return {
        "order_number": inp.order_number,
        "order_status": inp.order_status,
        "purchase_date": inp.purchase_date.isoformat(),
        "is_replacement": inp.is_replacement,
        "categories": sorted(inp.categories),
        "shipment": None
        if ship is None
        else {
            "status": ship.status,
            "shipped_on": ship.shipped_on.isoformat() if ship.shipped_on else None,
            "delivered_on": ship.delivered_on.isoformat() if ship.delivered_on else None,
            "carrier_expected_on": ship.carrier_expected_on.isoformat()
            if ship.carrier_expected_on
            else None,
            "has_delivery_exception": ship.has_delivery_exception,
        },
    }


def persist_determination(
    conn: Connection, ctx: CustomerContext, conversation_id: UUID, det: Determination
) -> UUID:
    """Write the determination, its applied clause keys and its policy document to the database
    (the caller's transaction). The clause keys must exist: a foreign key enforces it."""
    import json

    decision_id: UUID = conn.execute(
        text(
            "INSERT INTO decisions (conversation_id, customer_id, action_type, order_id, outcome,"
            " amount_cents, reason_codes, rule_set_version, input_state, finding)"
            " VALUES (:conv, :c, 'order_status', :o, :out, 0, CAST(:rc AS text[]), :rsv,"
            " CAST(:inp AS jsonb), :finding) RETURNING id"
        ),
        {
            "conv": conversation_id,
            "c": ctx.customer_id,
            "o": det.order_id,
            "out": det.decision_outcome,
            "rc": "{" + ",".join(det.reason_codes) + "}",
            "rsv": det.rule_set_version,
            "inp": json.dumps(
                {"state": det.input_state, "facts": det.result.facts}, sort_keys=True
            ),
            "finding": det.outcome,
        },
    ).scalar_one()
    for key in det.applied_clause_keys:
        conn.execute(
            text(
                "INSERT INTO decision_clauses (decision_id, customer_id, chunk_key)"
                " VALUES (:d, :c, :k)"
            ),
            {"d": decision_id, "c": ctx.customer_id, "k": key},
        )
    if det.applied_clause_keys:
        conn.execute(
            text(
                "INSERT INTO decision_policies (decision_id, policy_document_id)"
                " SELECT DISTINCT :d, d.id FROM policy_documents d JOIN policy_chunks c"
                " ON c.document_id = d.id WHERE c.chunk_key = ANY(CAST(:k AS text[]))"
            ),
            {"d": decision_id, "k": list(det.applied_clause_keys)},
        )
    return decision_id
