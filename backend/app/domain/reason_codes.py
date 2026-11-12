"""Reason codes recorded on every Decision / determination.

A reason code is a stable machine identifier, never prose. Rules emit them; handoffs carry them;
reports count them. They are shared across workflows so the refund and cancellation paths (M2)
extend this vocabulary instead of inventing a parallel one.
"""

from collections.abc import Sequence
from enum import StrEnum
from typing import Any


class ReasonCode(StrEnum):
    # --- order status (task 8) -----------------------------------------------------------------
    DELIVERED = "DELIVERED"
    DELIVERED_WITHIN_WINDOW = "DELIVERED_WITHIN_WINDOW"
    DELIVERED_AFTER_WINDOW = "DELIVERED_AFTER_WINDOW"
    WITHIN_DELIVERY_WINDOW = "WITHIN_DELIVERY_WINDOW"
    PAST_DELIVERY_WINDOW = "PAST_DELIVERY_WINDOW"
    NOT_YET_PRESUMED_LOST = "NOT_YET_PRESUMED_LOST"
    PRESUMED_LOST = "PRESUMED_LOST"
    UNDELIVERED_PAST_PRESUMED_LOST_DATE = "UNDELIVERED_PAST_PRESUMED_LOST_DATE"
    CARRIER_REPORTED_LOST = "CARRIER_REPORTED_LOST"
    CARRIER_DELIVERY_EXCEPTION = "CARRIER_DELIVERY_EXCEPTION"
    REPLACEMENT_OWN_WINDOW = "REPLACEMENT_OWN_WINDOW"
    NO_SHIPMENT_YET = "NO_SHIPMENT_YET"
    NO_PHYSICAL_SHIPMENT = "NO_PHYSICAL_SHIPMENT"
    ORDER_CANCELED = "ORDER_CANCELED"
    # --- safe-by-default refusals to guess ------------------------------------------------------
    POLICY_VERSION_NOT_IMPLEMENTED = "POLICY_VERSION_NOT_IMPLEMENTED"
    CATEGORY_TIMELINE_NOT_IMPLEMENTED = "CATEGORY_TIMELINE_NOT_IMPLEMENTED"
    # --- the standing rule on unverifiable claims (SPEC 5.7) -------------------------------------
    UNVERIFIABLE_CLAIM = "UNVERIFIABLE_CLAIM"
    # --- turn-level handoff / answer reasons ------------------------------------------------------
    EXPLANATION_MISMATCH = "EXPLANATION_MISMATCH"
    TOOL_CAP_EXCEEDED = "TOOL_CAP_EXCEEDED"
    CALL_BUDGET_EXCEEDED = "CALL_BUDGET_EXCEEDED"
    INJECTION_FLAGGED_REVIEW = "INJECTION_FLAGGED_REVIEW"
    ORDER_NOT_IDENTIFIED = "ORDER_NOT_IDENTIFIED"
    ORDER_NOT_FOUND = "ORDER_NOT_FOUND"
    INTENT_NOT_SUPPORTED_YET = "INTENT_NOT_SUPPORTED_YET"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


def unverifiable_claim_evidence(claims: Sequence[str]) -> dict[str, Any]:
    """Handoff evidence for the standing rule: ANY action whose outcome depends on a fact the
    system cannot verify from its own data goes to handoff with the customer's assertion recorded
    VERBATIM. The system never acts on an unchecked claim about state, and never silently ignores
    one either. Nothing here interprets, summarizes or corrects the claim."""
    return {
        "reason_code": ReasonCode.UNVERIFIABLE_CLAIM.value,
        "claims_verbatim": [str(c) for c in claims],
        "note": "the outcome depends on facts the system cannot verify from its own data",
    }
