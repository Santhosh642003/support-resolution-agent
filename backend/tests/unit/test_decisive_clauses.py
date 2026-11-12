"""Decisive versus supporting applied clauses (SPEC 10.5).

An explanation that cites only true clauses but omits the one that CAUSED the outcome is a wrong
explanation made of true statements. Every decisive clause must be cited or the turn fails closed;
omitting a supporting clause is allowed; citing a clause that was not applied still fails."""

from datetime import date
from uuid import uuid4

import pytest

from app.domain.reason_codes import ReasonCode
from app.domain.rules.order_status import (
    OrderStatusInput,
    OrderStatusOutcome,
    OrderStatusResult,
    ShipmentInput,
    determine_order_status,
)
from app.workflow.determination import Determination
from app.workflow.explanation import Explanation, verify_explanation

C1, C2, C3 = "shipping-v1-clause-1", "shipping-v1-clause-2", "shipping-v1-clause-3"
SHIPPED = date(2026, 3, 11)


def det_for(
    outcome: OrderStatusOutcome, applied: tuple[int, ...], decisive: tuple[int, ...]
) -> Determination:
    """A determination with chosen clause roles: the shape of any future Decision, e.g. a refund
    denied because of one clause while a different clause (the window) merely supports it."""
    result = OrderStatusResult(
        outcome,
        (ReasonCode.PRESUMED_LOST,),
        applied,
        1,
        {"as_of": "2026-04-15", "shipped_on": "2026-03-11"},
        decisive,
    )
    return Determination(uuid4(), "SIM-T", result, {})


def expl(
    keys: list[str], outcome: str = "presumed_lost", text: str = "Shipped on 2026-03-11."
) -> Explanation:
    return Explanation(outcome=outcome, cited_clause_keys=keys, explanation_text=text)  # type: ignore[arg-type]


def test_a_correct_explanation_that_omits_the_decisive_clause_fails_closed() -> None:
    det = det_for(OrderStatusOutcome.PRESUMED_LOST, (1, 2), (2,))  # clause 2 caused the outcome
    problems = verify_explanation(expl([C1]), det)  # cites only the (true) supporting window clause
    assert any("decisive" in p and C2 in p for p in problems)


def test_citing_every_decisive_clause_passes_even_without_the_supporting_ones() -> None:
    det = det_for(OrderStatusOutcome.PRESUMED_LOST, (1, 2, 3), (2,))
    assert verify_explanation(expl([C2]), det) == []  # supporting 1 and 3 omitted: allowed
    assert verify_explanation(expl([C2, C1, C3]), det) == []


def test_citing_a_clause_that_was_not_applied_still_fails_closed() -> None:
    det = det_for(OrderStatusOutcome.PRESUMED_LOST, (1, 2), (2,))
    problems = verify_explanation(expl([C2, "refund-v2-clause-1"]), det)
    assert any("did not apply" in p for p in problems)
    assert any("did not apply" in p for p in verify_explanation(expl([C2, C3]), det))  # applied? no


def test_every_decisive_clause_is_required_when_there_are_several() -> None:
    det = det_for(OrderStatusOutcome.LATE, (1, 2), (1, 2))
    problems = verify_explanation(expl([C1], outcome="late"), det)
    assert any(C2 in p for p in problems) and not any(C1 in p and "decisive" in p for p in problems)


def test_an_empty_citation_list_fails_when_something_was_decisive() -> None:
    det = det_for(OrderStatusOutcome.PRESUMED_LOST, (1, 2), (2,))
    assert any("decisive" in p for p in verify_explanation(expl([]), det))
    nothing = det_for(OrderStatusOutcome.DELIVERED_DIGITAL, (), ())
    assert (
        verify_explanation(expl([], outcome="delivered_digital", text="Delivered."), nothing) == []
    )


# --- what the rules mark as decisive ---------------------------------------------------------------


def run(
    shipment: ShipmentInput | None, today: date, replacement: bool = False
) -> OrderStatusResult:
    inp = OrderStatusInput(
        "SIM-T", "shipped", date(2026, 3, 10), replacement, frozenset({"general"}), shipment
    )
    return determine_order_status(inp, today, 1)


def ship(
    status: str = "in_transit", delivered: date | None = None, exception: bool = False
) -> ShipmentInput:
    return ShipmentInput(status, SHIPPED, delivered, date(2026, 3, 20), exception)


@pytest.mark.parametrize(
    ("today", "kwargs", "decisive", "supporting"),
    [
        (date(2026, 3, 13), {}, [C1], [C2]),  # on schedule: the window decides
        (date(2026, 3, 22), {}, [C1], [C2]),  # late: the window decides
        (date(2026, 3, 26), {}, [C2], [C1]),  # presumed lost: the presumed-lost rule decides
        (date(2026, 3, 23), {"exception": True, "status": "exception"}, [C2], [C1]),
        (date(2026, 4, 15), {"delivered": date(2026, 3, 18), "status": "delivered"}, [C1], []),
    ],
)
def test_the_rules_mark_the_clause_that_separates_the_outcome_from_its_neighbour(
    today: date, kwargs: dict[str, object], decisive: list[str], supporting: list[str]
) -> None:
    r = run(ship(**kwargs), today)  # type: ignore[arg-type]
    assert list(r.decisive_clause_keys) == decisive and list(r.supporting_clause_keys) == supporting
    assert set(r.decisive_clause_keys) <= set(r.applied_clause_keys)


def test_a_replacements_own_window_clause_is_supporting() -> None:
    r = run(ship(), date(2026, 3, 13), replacement=True)
    assert C3 in r.supporting_clause_keys and C3 not in r.decisive_clause_keys


def test_nothing_applied_means_nothing_decisive() -> None:
    r = determine_order_status(
        OrderStatusInput("SIM-T", "placed", date(2026, 3, 10), False, frozenset(), None),
        date(2026, 4, 1),
        1,
    )
    assert r.decisive_clause_keys == () and r.supporting_clause_keys == ()
