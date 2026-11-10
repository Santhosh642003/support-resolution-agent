"""Conflating the two policy anchors must be a mypy error, not a logic bug."""

import subprocess
import sys
import textwrap
from pathlib import Path

PRELUDE = """
from datetime import date
from app.domain.types import PurchaseDate, WindowAnchorDate

def refund_window_open(purchase: PurchaseDate, anchor: WindowAnchorDate, today: date) -> bool:
    return today >= anchor and today >= purchase
"""


def _mypy(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    f = tmp_path / "snippet.py"
    f.write_text(textwrap.dedent(PRELUDE) + textwrap.dedent(body))
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "mypy", "--strict", str(f)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_correct_call_typechecks(tmp_path: Path) -> None:
    r = _mypy(
        tmp_path,
        """
        refund_window_open(PurchaseDate(date(2026, 2, 20)), WindowAnchorDate(date(2026, 3, 10)), date.today())
    """,
    )
    assert r.returncode == 0, r.stdout


def test_swapped_arguments_are_rejected(tmp_path: Path) -> None:
    r = _mypy(
        tmp_path,
        """
        refund_window_open(WindowAnchorDate(date(2026, 3, 10)), PurchaseDate(date(2026, 2, 20)), date.today())
    """,
    )
    assert r.returncode != 0 and "arg-type" in r.stdout


def test_plain_date_is_rejected(tmp_path: Path) -> None:
    r = _mypy(
        tmp_path,
        """
        refund_window_open(date(2026, 2, 20), date(2026, 3, 10), date.today())
    """,
    )
    assert r.returncode != 0 and "arg-type" in r.stdout
