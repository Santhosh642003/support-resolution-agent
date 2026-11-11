"""Business-day arithmetic. The ONE place this definition lives (SPEC 16.4).

A business day is Monday to Friday. There is NO holiday calendar: a holiday calendar would be
more correct and add no engineering signal, and the simplification is stated here, in the README
limitations section and in the SPEC rather than left implicit.

Pure functions of dates; nothing here reads a clock.
"""

from datetime import date, timedelta


def is_business_day(d: date) -> bool:
    return d.weekday() < 5


def add_business_days(start: date, n: int) -> date:
    """The date `n` business days after `start`. `start` itself is day 0 and need not be a
    business day: adding 1 to a Saturday gives Monday, adding 10 to a Monday gives the Monday
    two weeks later."""
    if n < 0:
        raise ValueError("n must be non-negative")
    d = start
    while n > 0:
        d += timedelta(days=1)
        if is_business_day(d):
            n -= 1
    return d
