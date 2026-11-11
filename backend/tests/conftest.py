"""Suite-wide hooks."""

import pytest

from app.providers.adapters.fake import STALENESS_EVENTS


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    """Surface FakeAdapter staleness warnings (stale prompt hashes) so they are never invisible."""
    if STALENESS_EVENTS:
        terminalreporter.section("FakeAdapter staleness warnings")
        terminalreporter.write_line(f"{len(STALENESS_EVENTS)} stale-fixture warning(s):")
        for msg in dict.fromkeys(STALENESS_EVENTS):
            terminalreporter.write_line(f"  {msg}")
