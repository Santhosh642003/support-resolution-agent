"""Deterministic, non-model customer text. Used when the model must not (or cannot) speak:
a failed-closed explanation, a model outage, an order that cannot be identified, an intent this
milestone does not handle. The values come from the determination, never from a model.

Every customer-facing reply ends with the simulation notice and the data timestamp.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from app.workflow.determination import Determination


def footer(as_of: str, data_as_of: datetime) -> str:
    return (
        f"\n\n(This is a SIMULATED demo with synthetic data. Information as of {as_of}, "
        f"read at {data_as_of.strftime('%Y-%m-%d %H:%M')} UTC.)"
    )


_STATUS = {
    "delivered": "Order {order_number} was delivered on {delivered_on}.",
    "delivered_digital": (
        "Order {order_number} is a digital order and was delivered when you bought it."
    ),
    "in_transit_on_schedule": (
        "Order {order_number} shipped on {shipped_on} and is on its way. It is expected by "
        "{expected_on}; it is only treated as lost if it has not arrived by {presumed_lost_on}."
    ),
    "late": (
        "Order {order_number} shipped on {shipped_on} and has not arrived yet. It was expected by "
        "{expected_on}, so it is late. It is treated as lost if it has not arrived by "
        "{presumed_lost_on}."
    ),
    "presumed_lost": (
        "Order {order_number} shipped on {shipped_on} and has not arrived. It was expected by "
        "{expected_on} and is now treated as lost (the date for that was {presumed_lost_on})."
    ),
    "delivery_exception": (
        "Order {order_number} shipped on {shipped_on}, and the carrier has reported a delivery "
        "problem. It is treated as lost if it has not arrived by {presumed_lost_on}."
    ),
    "not_shipped": "Order {order_number} has not shipped yet.",
    "canceled": "Order {order_number} was canceled.",
}


def status_text(det: Determination) -> str:
    """The determination, in words, from a template. Safe to show when the model is not."""
    template = _STATUS.get(det.outcome)
    if template is None:
        return (
            f"I can't work out the status of order {det.order_number} automatically. "
            "A member of our team will look at it and follow up."
        )
    facts: dict[str, Any] = {k: v for k, v in det.result.facts.items() if v is not None}
    return template.format_map({**facts, "order_number": det.order_number})


# What happens next is a fact about the SYSTEM, so it is chosen here by outcome, never written by
# the model (F-009). Order status is read-only here: nothing is scheduled or monitored,
# nothing is changed. A line that says so is true; a promise would not be.
_NO_UPDATES = (
    "This demo does not send automatic updates; ask me again any time for the latest status."
)
_NO_CHANGE = "No refund, replacement or other change has been made to your order or account."
_CLOSING = {
    "in_transit_on_schedule": _NO_UPDATES,
    "late": _NO_UPDATES,
    "delivery_exception": _NO_UPDATES,
    "presumed_lost": _NO_CHANGE,
}  # delivered, delivered_digital, not_shipped, canceled: nothing further, so nothing is said


def closing_line(outcome: str, handoff_raised: bool) -> str:
    """The deterministic next-steps line, chosen by the outcome and by whether a handoff that the
    customer is being told about was raised. A review flag raised by Prompt Guard is internal and
    is not a handoff the customer is promised anything about, so callers pass False for it."""
    if handoff_raised:
        return handoff_notice()
    return _CLOSING.get(outcome, "")


def handoff_notice() -> str:
    return "I have passed this to a member of our team, who will follow up with you."


def not_found_text() -> str:
    return (
        "I couldn't find that order on your account. Please check the order number and try again."
    )


def not_identified_text(recent: Sequence[tuple[str, str]]) -> str:
    if not recent:
        return "I couldn't see which order you mean, and there are no orders on your account yet."
    lines = "\n".join(f"- {number} ({status})" for number, status in recent)
    return f"Which order do you mean? These are your most recent orders:\n{lines}"


def out_of_scope_text() -> str:
    return (
        "In this demo I can look up the status of an order. I can't help with that request yet. "
        "Tell me an order number and I will check where it is."
    )


def service_busy_text(pending: bool) -> str:
    return (
        "I couldn't process your message right now because the assistant service is busy. Please "
        "try again in a moment."
        if pending
        else "I couldn't process your message because the assistant service is unavailable. "
        "A member of our team can help instead."
    )
