"""Tool registry with hard caps: 8 tool calls per turn, 20 per conversation (SPEC 3).

A "tool call" is one deterministic read the workflow performs (an order lookup, a shipment read, a
policy retrieval). Every call is counted and timed and recorded in the trace; exceeding either cap
raises ToolCapExceeded before the call is made, and the workflow moves to HANDED_OFF.
"""

import time
from collections.abc import Callable
from typing import Any

PER_TURN_CAP = 8
PER_CONVERSATION_CAP = 20


class ToolCapExceeded(RuntimeError):
    def __init__(self, scope: str, used: int, cap: int) -> None:
        super().__init__(f"tool call cap exceeded: {used} used, {scope} cap {cap}")
        self.scope = scope


class ToolRegistry:
    def __init__(
        self,
        per_turn: int = PER_TURN_CAP,
        per_conversation: int = PER_CONVERSATION_CAP,
        used_before_in_conversation: int = 0,
        perf: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._per_turn = per_turn
        self._per_conversation = per_conversation
        self._before = used_before_in_conversation
        self._perf = perf
        self.used = 0
        self.log: list[dict[str, Any]] = []

    def call[T](self, name: str, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        if self.used >= self._per_turn:
            raise ToolCapExceeded("per-turn", self.used, self._per_turn)
        if self._before + self.used >= self._per_conversation:
            raise ToolCapExceeded(
                "per-conversation", self._before + self.used, self._per_conversation
            )
        self.used += 1
        started = self._perf()
        entry: dict[str, Any] = {"tool": name, "ok": True}
        self.log.append(entry)
        try:
            return fn(*args, **kwargs)
        except BaseException:
            entry["ok"] = False
            raise
        finally:
            entry["ms"] = round((self._perf() - started) * 1000, 2)
