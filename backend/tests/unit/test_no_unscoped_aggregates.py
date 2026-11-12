"""Structural guard for FAILURE_ANALYSIS F-006: a test must not assert over a whole shared table.

An aggregate (count, sum, max, min, avg) over a table with no WHERE clause reads rows committed by
other tests, so it is true or false depending on test order. Use `tests/db/scoped.py`, which makes
the scope a mandatory argument. The only unscoped aggregates allowed are listed in ALLOWED, each
with the reason it cannot be polluted.
"""

import ast
import re
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]
AGGREGATE = re.compile(r"(?i)\b(count|sum|max|min|avg)\s*\(")
FROM = re.compile(r"(?i)\bfrom\b")
WHERE = re.compile(r"(?i)\bwhere\b")

# substring of the SQL -> why it cannot be polluted by another test
ALLOWED = {
    "generate_series": "a per-day sweep over policy reference tables, grouped; no test commits policy rows",
    "information_schema": "database catalog, not test data",
    "pg_": "database catalog, not test data",
}


def _sql_strings(tree: ast.AST) -> list[tuple[int, str]]:
    """Every string a test hands to the database, with f-strings re-joined so a WHERE that sits in
    a later fragment of the same statement is seen."""
    found: list[tuple[int, str]] = []
    consumed: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            parts = [v.value if isinstance(v, ast.Constant) else "{x}" for v in node.values]
            found.append((node.lineno, "".join(str(p) for p in parts)))
            consumed.update(id(v) for v in node.values)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in consumed
        ):
            found.append((node.lineno, node.value))
    return found


def unscoped_aggregates(source: str) -> list[tuple[int, str]]:
    bad = []
    for line, sql in _sql_strings(ast.parse(source)):
        if AGGREGATE.search(sql) and FROM.search(sql) and not WHERE.search(sql):
            if not any(token in sql for token in ALLOWED):
                bad.append((line, sql[:80]))
    return bad


def test_no_test_writes_an_unscoped_aggregate_over_a_shared_table() -> None:
    offenders = {}
    for path in TESTS.rglob("*.py"):
        if path.name in {"scoped.py", Path(__file__).name}:
            continue
        bad = unscoped_aggregates(path.read_text(encoding="utf-8"))
        if bad:
            offenders[str(path.relative_to(TESTS))] = bad
    assert not offenders, f"unscoped aggregates (use tests/db/scoped.py, F-006): {offenders}"


def test_the_checker_detects_the_unsafe_pattern_and_accepts_the_safe_ones() -> None:
    assert unscoped_aggregates('q = "SELECT count(*) FROM refunds"')
    assert unscoped_aggregates('q = "SELECT sum(amount_cents) FROM refunds"')
    assert not unscoped_aggregates('q = "SELECT count(*) FROM refunds WHERE customer_id = :c"')
    assert not unscoped_aggregates('t = "x"\nq = f"SELECT count(*) FROM {t} WHERE id = :i"')
    assert unscoped_aggregates('t = "x"\nq = f"SELECT count(*) FROM {t}"')
    assert not unscoped_aggregates('q = "SELECT count(DISTINCT v) FROM generate_series(1,3) g"')
    assert not unscoped_aggregates('q = "SELECT 1"')


def test_the_scoped_helpers_refuse_an_empty_or_malformed_scope() -> None:
    import pytest

    from tests.db.scoped import Scope

    with pytest.raises(ValueError):
        Scope.customers()  # no 'everything' scope exists
    with pytest.raises(ValueError):
        Scope.ids("id; DROP TABLE x", 1)
