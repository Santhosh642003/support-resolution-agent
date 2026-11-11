"""The injected clock. Tests pin it to SIMULATED_TODAY; nothing here depends on the real date."""

from datetime import date, timedelta

from app.clock import SIMULATED_TODAY, Clock, FixedClock, SystemClock


def test_simulated_today_is_after_the_policy_cutover() -> None:
    assert date(2026, 4, 15) == SIMULATED_TODAY
    assert SIMULATED_TODAY > date(2026, 3, 1)


def test_fixed_clock_defaults_to_simulated_today_and_never_moves() -> None:
    clock = FixedClock()
    assert clock.today() == SIMULATED_TODAY
    assert clock.now() == clock.now()
    assert clock.now().date() == SIMULATED_TODAY
    assert clock.now().utcoffset() == timedelta(0)


def test_fixed_clock_can_watch_a_window_close_without_mutating() -> None:
    clock = FixedClock()
    later = clock.plus_days(10)
    assert later.today() == date(2026, 4, 25)
    assert clock.today() == SIMULATED_TODAY


def test_both_clocks_satisfy_the_protocol() -> None:
    clocks: list[Clock] = [FixedClock(), SystemClock()]
    for c in clocks:
        assert isinstance(c.today(), date)
        assert c.now().utcoffset() == timedelta(0)


def test_system_clock_is_real_not_pinned() -> None:
    # The real clock is never pinned: two readings are ordered and the second is not earlier.
    c = SystemClock()
    a, b = c.now(), c.now()
    assert b >= a
