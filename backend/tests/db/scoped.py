"""Scoped counting and reading helpers for tests that share a database (FAILURE_ANALYSIS F-006).

Some tests must commit (a real concurrency test needs real connections), and the provider layer
writes its counters in their own committed transactions, so a shared test database holds rows from
other tests. An assertion over a whole table is true or false depending on test order. These
helpers make the SCOPE a mandatory argument: there is no way to call them without saying whose rows
(or which ids) the assertion is about. `tests/unit/test_no_unscoped_aggregates.py` fails if a test
writes an unscoped aggregate query by hand, so the unsafe pattern takes deliberate effort.
"""

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def _ident(name: str) -> str:
    if not _IDENT.match(name):
        raise ValueError(f"not a plain SQL identifier: {name!r}")
    return name


@dataclass(frozen=True)
class Scope:
    """Which rows an assertion is about. Mandatory; there is no default and no 'everything'."""

    column: str
    values: tuple[Any, ...]

    def __post_init__(self) -> None:
        _ident(self.column)
        if not self.values:
            raise ValueError("a Scope needs at least one value: an empty scope would match nothing")

    @classmethod
    def customers(cls, *customer_ids: UUID) -> "Scope":
        return cls("customer_id", tuple(customer_ids))

    @classmethod
    def ids(cls, column: str, *values: Any) -> "Scope":
        return cls(column, tuple(values))

    def where(self) -> str:
        return f"{self.column} = ANY(:scope_values)"

    def params(self) -> dict[str, Any]:
        return {"scope_values": list(self.values)}


def count_rows(conn: Connection, table: str, scope: Scope) -> int:
    sql = f"SELECT count(*) FROM {_ident(table)} WHERE {scope.where()}"  # noqa: S608
    return int(conn.execute(text(sql), scope.params()).scalar_one())


def sum_column(conn: Connection, table: str, column: str, scope: Scope) -> int:
    sql = f"SELECT coalesce(sum({_ident(column)}), 0) FROM {_ident(table)} WHERE {scope.where()}"  # noqa: S608
    return int(conn.execute(text(sql), scope.params()).scalar_one())


def fetch_rows(conn: Connection, table: str, scope: Scope, columns: str = "*") -> list[Any]:
    cols = ", ".join(_ident(c.strip()) for c in columns.split(",")) if columns != "*" else "*"
    sql = f"SELECT {cols} FROM {_ident(table)} WHERE {scope.where()}"  # noqa: S608
    return list(conn.execute(text(sql), scope.params()))
