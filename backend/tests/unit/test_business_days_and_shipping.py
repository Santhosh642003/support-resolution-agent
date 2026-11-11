"""Business-day arithmetic (Monday to Friday, no holiday calendar) and the shipping-policy dates.
The shipping constants are checked against the stored policy text, so they cannot silently drift."""

import re
from datetime import date

import pytest

from app.domain.rules.business_days import add_business_days, is_business_day
from app.domain.rules.shipping import (
    DELIVERY_WINDOW_BUSINESS_DAYS,
    PRESUMED_LOST_BUSINESS_DAYS,
    SHIPPING_POLICY_VERSION,
    expected_delivery_date,
    presumed_lost_date,
)
from app.retrieval.policy_loader import load_policies

MON = date(2026, 3, 2)  # a Monday


def test_weekends_are_not_business_days() -> None:
    assert [is_business_day(date(2026, 3, d)) for d in range(2, 9)] == [True] * 5 + [False] * 2


@pytest.mark.parametrize(
    ("start", "n", "expected"),
    [
        (MON, 0, MON),
        (MON, 1, date(2026, 3, 3)),
        (MON, 5, date(2026, 3, 9)),  # a full week later
        (MON, 10, date(2026, 3, 16)),  # two weeks later
        (date(2026, 3, 6), 1, date(2026, 3, 9)),  # Friday + 1 skips the weekend
        (date(2026, 3, 7), 1, date(2026, 3, 9)),  # Saturday + 1 is Monday
        (date(2026, 3, 8), 1, date(2026, 3, 9)),  # Sunday + 1 is Monday
        (date(2026, 3, 7), 0, date(2026, 3, 7)),  # day 0 is the start itself
    ],
)
def test_add_business_days(start: date, n: int, expected: date) -> None:
    assert add_business_days(start, n) == expected


def test_negative_days_are_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        add_business_days(MON, -1)


def test_shipping_dates_follow_the_policy() -> None:
    assert expected_delivery_date(date(2026, 3, 11)) == date(2026, 3, 20)
    assert presumed_lost_date(date(2026, 3, 11)) == date(2026, 3, 25)


def test_constants_match_the_stored_shipping_policy_text() -> None:
    (doc,) = [d for d in load_policies() if d.policy_type == "shipping"]
    assert doc.version == SHIPPING_POLICY_VERSION
    text = {c.number: c.text for c in doc.clauses}
    window = int(re.search(r"(\d+) business days", text[1]).group(1))  # type: ignore[union-attr]
    lost = int(re.search(r"(\d+) business days", text[2]).group(1))  # type: ignore[union-attr]
    assert (window, lost) == (DELIVERY_WINDOW_BUSINESS_DAYS, PRESUMED_LOST_BUSINESS_DAYS)
