"""Tool counting is by capability; writes always count individually (SPEC 10.5)."""

import pytest

from app.workflow.tools import ToolCapExceeded, ToolRegistry


def test_a_read_capability_counts_once_however_many_statements_it_runs() -> None:
    reg = ToolRegistry()
    reg.call("get_order_snapshot", lambda: [1, 2, 3, 4])
    assert reg.used == 1 and reg.writes_used == 0 and reg.log[0]["kind"] == "read"


def test_every_side_effecting_execution_counts_individually_and_always() -> None:
    reg = ToolRegistry(per_turn=8)
    for i in range(3):
        reg.execute("refund", lambda i=i: i)  # three writes are three counts, never one batch
    assert reg.used == 3 and reg.writes_used == 3
    assert [e["kind"] for e in reg.log] == ["write"] * 3


def test_the_cap_bounds_writes_and_a_refused_write_is_not_counted_as_done() -> None:
    reg = ToolRegistry(per_turn=2)
    reg.execute("a", lambda: None)
    reg.execute("b", lambda: None)
    ran: list[int] = []
    with pytest.raises(ToolCapExceeded):
        reg.execute("c", lambda: ran.append(1))
    assert ran == [] and reg.writes_used == 2  # refused BEFORE it ran, and not counted


def test_reads_and_writes_share_one_cap() -> None:
    reg = ToolRegistry(per_turn=2)
    reg.call("read", lambda: None)
    reg.execute("write", lambda: None)
    with pytest.raises(ToolCapExceeded):
        reg.call("read2", lambda: None)
