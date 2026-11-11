"""No module may read the real clock directly; business date logic takes an injected Clock.

Same idea as the httpx2 import boundary: enforced by CI instead of remembered. Scope is the
whole of app/ (except the one allow-listed module that IS the real clock) and tests/ (a test
that depends on the real date is a bug; tests pin the clock to SIMULATED_TODAY).

Limits, stated plainly: this is a static check. It resolves aliases of the datetime module and
of the date/datetime classes, and flags attribute access as well as calls (so a
`default_factory=datetime.now` is caught). It does not see `getattr(datetime, "now")`,
`datetime.fromtimestamp(time.time())`, or SQL `CURRENT_DATE`; reviewers still have to.
Confirmation-token expiry deliberately uses the database clock_timestamp() and is out of scope.
"""

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
SCANNED = (BACKEND / "app", BACKEND / "tests")
# The only module allowed to read the real clock: it implements SystemClock.
ALLOWED = {BACKEND / "app" / "clock.py"}

BANNED_ATTRS = {"today", "now", "utcnow"}
CLASSES = {"date", "datetime"}


def find_direct_clock_reads(source: str) -> list[tuple[int, str]]:
    tree = ast.parse(source)
    module_aliases: set[str] = set()  # names bound to the datetime MODULE
    class_aliases: set[str] = set()  # names bound to datetime.date / datetime.datetime
    hits: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "datetime":
                    module_aliases.add(a.asname or "datetime")
        elif isinstance(node, ast.ImportFrom) and node.module == "datetime" and node.level == 0:
            for a in node.names:
                if a.name == "*":
                    hits.append((node.lineno, "from datetime import * (cannot be analysed)"))
                elif a.name in CLASSES:
                    class_aliases.add(a.asname or a.name)

    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute) and node.attr in BANNED_ATTRS):
            continue
        base = node.value
        is_class = isinstance(base, ast.Name) and base.id in class_aliases
        is_module_class = (
            isinstance(base, ast.Attribute)
            and base.attr in CLASSES
            and isinstance(base.value, ast.Name)
            and base.value.id in module_aliases
        )
        if is_class or is_module_class:
            hits.append((node.lineno, ast.unparse(node)))
    return sorted(hits)


def test_no_module_reads_the_real_clock_directly() -> None:
    offenders: dict[str, list[tuple[int, str]]] = {}
    for root in SCANNED:
        for path in root.rglob("*.py"):
            if path in ALLOWED:
                continue
            hits = find_direct_clock_reads(path.read_text(encoding="utf-8"))
            if hits:
                offenders[str(path.relative_to(BACKEND))] = hits
    assert not offenders, (
        f"direct clock reads found: {offenders}. Take a Clock parameter (app/clock.py) instead."
    )


def test_the_allow_list_is_exactly_the_clock_module_and_it_does_read_the_clock() -> None:
    (clock_module,) = ALLOWED
    assert clock_module.is_file()
    assert find_direct_clock_reads(clock_module.read_text(encoding="utf-8")), (
        "if the clock module no longer reads the real clock, drop the allow-list entry"
    )


# --- the checker itself must actually detect what it claims -------------------------------


def _detects(source: str) -> bool:
    return bool(find_direct_clock_reads(source))


def test_detects_plain_and_aliased_forms() -> None:
    assert _detects("from datetime import date\nx = date.today()")
    assert _detects("from datetime import datetime\nx = datetime.now()")
    assert _detects("from datetime import datetime\nx = datetime.utcnow()")
    assert _detects("from datetime import datetime as D\nx = D.now()")
    assert _detects("import datetime\nx = datetime.datetime.now()")
    assert _detects("import datetime as dt\nx = dt.date.today()")
    assert _detects("import datetime as dt\nx = dt.datetime.utcnow()")


def test_detects_references_that_are_not_calls() -> None:
    assert _detects("from datetime import datetime\nf = datetime.now")
    assert _detects(
        "from dataclasses import field\nfrom datetime import datetime\n"
        "x = field(default_factory=datetime.now)"
    )


def test_detects_star_import() -> None:
    assert _detects("from datetime import *\n")


def test_allows_pure_date_arithmetic_and_injected_clocks() -> None:
    assert not _detects("from datetime import date, datetime, UTC\nx = date(2026, 4, 15)")
    assert not _detects("from datetime import datetime, UTC\nx = datetime(2026, 1, 1, tzinfo=UTC)")
    assert not _detects("def f(clock):\n    return clock.today()")
    assert not _detects("from datetime import date\nx = date.fromisoformat('2026-04-15')")
    assert not _detects("class A:\n    def now(self): ...\nA().now()")
