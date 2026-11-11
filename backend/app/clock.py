"""The injected clock for business date logic.

NO module under app/ may call date.today(), datetime.now() or datetime.utcnow() directly
(enforced by tests/unit/test_no_direct_clock.py). Anything that needs "today" takes a `Clock`
as a parameter. This module is the single allow-listed place that reads the real clock.

Why: policy windows are measured in days (30/45-day refunds, 60-day proration, 14-day
cooling-off, 10-business-day presumed-lost). Seed data uses absolute dates; if the system read
the real clock, every window would silently expire as real time passed and the test suite
would rot. If seed dates were computed relative to the real clock instead, the evaluation
suite would stop being reproducible. So tests and the evaluation runner pin the clock to
SIMULATED_TODAY.

What this does NOT govern: confirmation-token expiry stays on the database's clock_timestamp()
(real wall clock). It operates on a timescale of minutes inside a single request, and a pinned
clock there would make an expired token look valid. Do not unify the two (docs/SPEC.md 2.5).
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol

# The simulated current date for seed data, tests and evaluation. After the 2026-03-01 policy
# cutover, so both policy versions have orders inside and outside their windows.
SIMULATED_TODAY = date(2026, 4, 15)

# FixedClock.now() is noon UTC: far from any midnight, so UTC and nearby-timezone calendar
# dates agree.
_NOON_UTC = time(12, 0, tzinfo=UTC)


class Clock(Protocol):
    def today(self) -> date: ...

    def now(self) -> datetime:
        """A timezone-aware (UTC) instant."""
        ...


class SystemClock:
    """Production clock: the real wall clock, in UTC."""

    def today(self) -> date:
        return datetime.now(UTC).date()

    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True)
class FixedClock:
    """Deterministic clock for tests and evaluation. Defaults to SIMULATED_TODAY."""

    fixed_today: date = SIMULATED_TODAY

    def today(self) -> date:
        return self.fixed_today

    def now(self) -> datetime:
        return datetime.combine(self.fixed_today, _NOON_UTC)

    def plus_days(self, days: int) -> "FixedClock":
        """A new clock `days` later, for tests that watch a window close."""
        return FixedClock(self.fixed_today + timedelta(days=days))
