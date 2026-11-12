"""An explicit state machine: named states, an allow-list of transitions, and a history.

Not a chain of conditionals. Every state change goes through `StateMachine.to`, which refuses a
transition that is not in `ALLOWED` and records why. The states are the SPEC 3 states used by the
read-only order-status workflow. One deliberate deviation from the SPEC's first sketch: DATA_FETCHED
comes BEFORE POLICY_RETRIEVED, because retrieval scope is narrowed by facts read from the order
(SPEC 7.3), so the order has to be fetched first.

An unauthenticated request never leaves NEW (the only way out of NEW is AUTHENTICATED). Any
non-terminal state can move to HANDED_OFF (tool cap, call budget, mismatch, unrecoverable error).
"""

from dataclasses import dataclass
from enum import StrEnum


class State(StrEnum):
    NEW = "NEW"
    AUTHENTICATED = "AUTHENTICATED"
    INTENT_CLASSIFIED = "INTENT_CLASSIFIED"
    DATA_FETCHED = "DATA_FETCHED"
    POLICY_RETRIEVED = "POLICY_RETRIEVED"
    DECIDED = "DECIDED"
    ANSWERED = "ANSWERED"
    HANDED_OFF = "HANDED_OFF"
    PENDING = "PENDING"
    FAILED = "FAILED"


TERMINAL = frozenset({State.ANSWERED, State.HANDED_OFF, State.PENDING, State.FAILED})

ALLOWED: dict[State, frozenset[State]] = {
    State.NEW: frozenset({State.AUTHENTICATED}),
    State.AUTHENTICATED: frozenset(
        {State.INTENT_CLASSIFIED, State.PENDING, State.FAILED, State.HANDED_OFF}
    ),
    State.INTENT_CLASSIFIED: frozenset({State.DATA_FETCHED, State.ANSWERED, State.HANDED_OFF}),
    State.DATA_FETCHED: frozenset({State.POLICY_RETRIEVED, State.ANSWERED, State.HANDED_OFF}),
    State.POLICY_RETRIEVED: frozenset({State.DECIDED, State.HANDED_OFF}),
    State.DECIDED: frozenset({State.ANSWERED, State.HANDED_OFF}),
    State.ANSWERED: frozenset(),
    State.HANDED_OFF: frozenset(),
    State.PENDING: frozenset(),
    State.FAILED: frozenset(),
}


class IllegalTransition(RuntimeError):
    pass


@dataclass(frozen=True)
class Transition:
    from_state: State
    to_state: State
    reason: str


class StateMachine:
    def __init__(self) -> None:
        self.state = State.NEW
        self.history: list[Transition] = []

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL

    def to(self, new: State, reason: str) -> None:
        if new not in ALLOWED[self.state]:
            raise IllegalTransition(f"{self.state} -> {new} is not allowed ({reason})")
        self.history.append(Transition(self.state, new, reason))
        self.state = new
