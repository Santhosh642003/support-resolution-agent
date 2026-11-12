# ruff: noqa: E501  (long SQL strings and trace keys; wrapping them hurts readability)
"""The order-status workflow (read-only; M1). One turn = one customer message.

    NEW -> AUTHENTICATED -> INTENT_CLASSIFIED -> DATA_FETCHED -> POLICY_RETRIEVED -> DECIDED
        -> ANSWERED | HANDED_OFF            (PENDING / FAILED when the model service is down)

Who does what:
  * CODE decides everything that matters: which order (matched against THIS customer's own orders),
    the determination (shipping rules, injected clock), which policies are in scope (facts from the
    order), which clauses are cited (the determination's applied clause keys).
  * The MODEL is called exactly twice on the full path (CallBudget(2), asserted): extraction
    (reasoning_effort low) and explanation (default effort, max_tokens 1500). It explains; it never
    chooses an order, a scope, a citation or an outcome. Its stated outcome is compared against the
    determination and any disagreement fails the turn closed.
  * Prompt Guard scans the customer message. Detection only: it annotates, may raise a
    non-blocking review, never relaxes a check and is never the reason a turn is blocked.

No business state is written: the only writes are the record of the conversation itself (messages,
the determination, handoffs, the trace, audit rows). A refund, a cancellation or any action is not
reachable from here.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, text

from app.auth.context import CustomerContext
from app.clock import Clock
from app.db.rls import set_current_customer
from app.domain.reason_codes import ReasonCode
from app.domain.rules.order_status import (
    OrderStatusInput,
    OrderStatusOutcome,
    OrderStatusResult,
    ShipmentInput,
    determine_order_status,
)
from app.domain.types import PurchaseDate
from app.providers.base import CallBudgetExceeded, CallTrace, LLMError, to_workflow_status
from app.providers.client import CallBudget, LLMClient
from app.providers.guard import GuardVerdict, PromptGuard
from app.repositories import state as repo
from app.retrieval.embedder import Embedder
from app.retrieval.hybrid import HybridConfig, RetrievalResult
from app.retrieval.policy_store import PolicyClause
from app.retrieval.scope import decide_scope, scope_facts_from_state
from app.workflow import templates
from app.workflow.determination import Determination, input_state_of, persist_determination
from app.workflow.explanation import (
    Explanation,
    commitment_phrases,
    explanation_request,
    verify_explanation,
)
from app.workflow.extraction import Extraction, candidate_order_numbers, extraction_request
from app.workflow.policy_context import policy_context
from app.workflow.state_machine import State, StateMachine
from app.workflow.tools import PER_CONVERSATION_CAP, PER_TURN_CAP, ToolCapExceeded, ToolRegistry

MODEL_CALLS_PER_CASE = 2


class ConversationNotFound(LookupError):
    """Not found and not yours are the same error (SPEC 2.2)."""


@dataclass(frozen=True)
class Citation:
    clause_key: str
    label: str  # e.g. "Shipping Policy v1, clause 2"
    text: str


@dataclass(frozen=True)
class TurnResult:
    conversation_id: UUID
    turn_no: int
    trace_id: str
    state: State
    reply: str
    citations: tuple[Citation, ...]
    as_of: date
    data_as_of: datetime
    determination: dict[str, Any] | None
    decision_id: UUID | None
    handoff_reason: str | None
    review_requested: bool
    degraded: bool
    model_calls: int
    tool_calls: int
    trace: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Out:
    reply: str
    citations: tuple[Citation, ...] = ()
    determination: Determination | None = None
    decision_id: UUID | None = None
    handoff_reason: str | None = None
    answer_reason: str | None = None
    degraded: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)


def _on(ts: datetime | None) -> date | None:
    return None if ts is None else ts.astimezone(UTC).date()


def _llm_entry(t: CallTrace | None) -> dict[str, Any]:
    if t is None:
        return {}
    return {
        "cache": t.cache,
        "adapter_calls": t.adapter_calls,
        "transport_attempts": t.transport_attempts,
        "native_fallbacks": t.native_fallbacks,
        "prompt_tokens": t.prompt_tokens,
        "completion_tokens": t.completion_tokens,
        "model_ms": round(t.model_ms, 1),
        "total_ms": round(t.total_ms, 1),
        "llm_trace_id": t.trace_id,
    }


class OrderStatusWorkflow:
    def __init__(
        self,
        client: LLMClient,
        embedder: Embedder,
        clock: Clock,
        hybrid: HybridConfig,
        *,
        guard: PromptGuard | None = None,
        explanation_max_tokens: int = 1500,
        per_turn_tools: int = PER_TURN_CAP,
        per_conversation_tools: int = PER_CONVERSATION_CAP,
        perf: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._client = client
        self._embedder = embedder
        self._clock = clock
        self._hybrid = hybrid
        self._guard = guard
        self._max_tokens = explanation_max_tokens
        self._per_turn = per_turn_tools
        self._per_conv = per_conversation_tools
        self._perf = perf

    # ------------------------------------------------------------------------------------------
    def run_turn(
        self,
        conn: Connection,
        ctx: CustomerContext,
        message: str,
        *,
        conversation_id: UUID | None = None,
        scenario_id: str | None = None,
    ) -> TurnResult:
        set_current_customer(conn, ctx.customer_id)  # bind the identity to this transaction too
        started = self._perf()
        now = self._clock.now()
        today = self._clock.today()
        trace: dict[str, Any] = {
            "trace_id": uuid4().hex,
            "scenario_id": scenario_id,
            "as_of": today.isoformat(),
        }
        conv_id, turn_no, used_before = self._open_conversation(conn, ctx, conversation_id)
        machine = StateMachine()
        tools = ToolRegistry(self._per_turn, self._per_conv, used_before)
        budget = CallBudget(MODEL_CALLS_PER_CASE)
        machine.to(State.AUTHENTICATED, "valid session")

        verdict = self._scan(message)
        trace["prompt_guard"] = verdict.model_dump()
        label = "unscanned" if not verdict.available else "flagged" if verdict.flagged else "clean"
        self._store_message(conn, ctx, conv_id, "customer", message, label)
        review = bool(verdict.flagged)
        if review:  # a non-blocking review flag; the turn is answered normally
            self._record_handoff(
                conn,
                ctx,
                conv_id,
                "review",
                ReasonCode.INJECTION_FLAGGED_REVIEW.value,
                {
                    "customer_message_verbatim": message,
                    "guard": verdict.model_dump(),
                    "trace_id": trace["trace_id"],
                },
            )

        try:
            out = self._pipeline(
                conn, ctx, conv_id, message, scenario_id, today, now, machine, tools, budget, trace
            )
        except ToolCapExceeded as exc:
            out = self._fail_handoff(machine, ReasonCode.TOOL_CAP_EXCEEDED, str(exc), message)
        except Exception as exc:  # fail closed: never a half-built reply, never a claimed success
            out = self._fail_handoff(
                machine,
                ReasonCode.INTERNAL_ERROR,
                f"{type(exc).__name__}: {str(exc)[:200]}",
                message,
            )
        if out.handoff_reason:
            out.evidence.setdefault("customer_message_verbatim", message)
            out.evidence.setdefault("claims_verbatim", trace.get("claims", []))
            out.evidence["actions_attempted"] = []  # a read-only workflow attempts none
            out.evidence["trace_id"] = trace["trace_id"]
            out.evidence["tool_calls"] = list(tools.log)
            self._record_handoff(conn, ctx, conv_id, "handoff", out.handoff_reason, out.evidence)

        reply = out.reply + templates.footer(today.isoformat(), now)
        self._store_message(conn, ctx, conv_id, "assistant", reply, "clean")
        llm = [
            trace.get("extraction", {}).get("llm", {}),
            trace.get("explanation", {}).get("llm", {}),
        ]
        trace.update(
            {
                "transitions": [
                    {"from": t.from_state.value, "to": t.to_state.value, "reason": t.reason}
                    for t in machine.history
                ],
                "tools": tools.log,
                "tool_calls": tools.used,
                "model_calls": budget.used,
                "tokens": {
                    "prompt": sum(e.get("prompt_tokens") or 0 for e in llm),
                    "completion": sum(e.get("completion_tokens") or 0 for e in llm),
                },
                "model_ms": round(sum(e.get("model_ms", 0.0) for e in llm), 1),
                "native_fallbacks": sum(e.get("native_fallbacks", 0) for e in llm),
                "total_ms": round((self._perf() - started) * 1000, 1),
                "final_state": machine.state.value,
                "reply_source": "model"
                if trace.get("explanation", {}).get("accepted")
                else "template",
                "handoff_reason": out.handoff_reason,
                "answer_reason": out.answer_reason,
                "degraded": out.degraded,
                "review_requested": review,
            }
        )
        if budget.used > MODEL_CALLS_PER_CASE:  # CallBudget already refuses; this is the assertion
            raise RuntimeError(
                f"{budget.used} model calls in one case (cap {MODEL_CALLS_PER_CASE})"
            )
        self._finalize(conn, ctx, conv_id, turn_no, machine, tools, budget, trace)
        return TurnResult(
            conv_id,
            turn_no,
            trace["trace_id"],
            machine.state,
            reply,
            out.citations,
            today,
            now,
            None if out.determination is None else out.determination.as_dict(),
            out.decision_id,
            out.handoff_reason,
            review,
            out.degraded,
            budget.used,
            tools.used,
            trace,
        )

    # ------------------------------------------------------------------------------------------
    def _pipeline(
        self,
        conn: Connection,
        ctx: CustomerContext,
        conv_id: UUID,
        message: str,
        scenario_id: str | None,
        today: date,
        now: datetime,
        machine: StateMachine,
        tools: ToolRegistry,
        budget: CallBudget,
        trace: dict[str, Any],
    ) -> _Out:
        # ---- model call 1: extraction -----------------------------------------------------------
        try:
            got = self._client.complete_structured(
                extraction_request(message, scenario_id), Extraction, budget=budget
            )
        except CallBudgetExceeded as exc:
            return self._to_handoff(machine, ReasonCode.CALL_BUDGET_EXCEEDED, str(exc))
        except LLMError as exc:
            pending = to_workflow_status(exc) == "pending"
            machine.to(State.PENDING if pending else State.FAILED, type(exc).__name__)
            trace["extraction"] = {
                "error": type(exc).__name__,
                "detail": exc.detail or str(exc)[:300],
            }
            return _Out(
                templates.service_busy_text(pending),
                answer_reason=ReasonCode.MODEL_UNAVAILABLE.value,
            )
        ext = got.parsed
        trace["extraction"] = {
            "intent": ext.intent,
            "order_number": ext.order_number,
            "confidence": ext.confidence,
            "attempts": got.attempts,
            "llm": _llm_entry(got.raw.trace),
        }
        trace["claims"] = list(ext.customer_claims)  # verbatim: never acted on, never ignored
        machine.to(State.INTENT_CLASSIFIED, f"intent={ext.intent}")
        if ext.intent != "order_status":
            machine.to(State.ANSWERED, "intent not handled in this milestone")
            return _Out(
                templates.out_of_scope_text(),
                answer_reason=ReasonCode.INTENT_NOT_SUPPORTED_YET.value,
            )

        # ---- resolve the order in CODE, among this customer's own orders ----------------------------
        candidates = candidate_order_numbers(ext.order_number, message)
        snap = None
        for number in candidates:  # one consistent snapshot per candidate, at most two (SPEC 10.5)
            snap = tools.call("get_order_snapshot", repo.order_snapshot, conn, ctx, number)
            if snap is not None:
                break
        trace["resolution"] = {
            "candidates": candidates,
            "matched": None if snap is None else snap.order.order_number,
        }
        if snap is None:
            if candidates:  # not found and not yours are indistinguishable
                machine.to(State.ANSWERED, "order not found")
                return _Out(
                    templates.not_found_text(), answer_reason=ReasonCode.ORDER_NOT_FOUND.value
                )
            recent = tools.call("list_orders", repo.list_orders, conn, ctx, 5)
            machine.to(State.ANSWERED, "order not identified")
            return _Out(
                templates.not_identified_text([(r.order_number, str(r.status)) for r in recent]),
                answer_reason=ReasonCode.ORDER_NOT_IDENTIFIED.value,
            )

        # ---- live state: already read as one snapshot ---------------------------------------------------
        order = snap.order
        ship, categories = snap.shipment, snap.categories
        purchase = PurchaseDate(order.purchase_date)
        shipped_on = _on(ship.shipped_at) if ship else None
        delivered_on = _on(ship.delivered_at) if ship else None
        inp = OrderStatusInput(
            order_number=order.order_number,
            order_status=str(order.status),
            purchase_date=order.purchase_date,
            is_replacement=order.replacement_for_number is not None,
            categories=categories,
            shipment=None
            if ship is None
            else ShipmentInput(
                str(ship.status),
                shipped_on,
                delivered_on,
                _on(ship.expected_at),
                str(ship.status) == "exception" or "DELIVERY_EXCEPTION" in snap.event_codes,
            ),
        )
        machine.to(State.DATA_FETCHED, f"order {order.order_number}")

        # ---- scope from FACTS, then retrieval (context text only) ----------------------------------------
        facts = scope_facts_from_state(
            categories,
            str(order.status),
            None if ship is None else str(ship.status),
            shipped_on,
            delivered_on,
            ship is not None,
            today,
        )
        scope = decide_scope(["order_status"], facts)
        pctx = tools.call(
            "policy_context",
            policy_context,
            conn,
            self._embedder,
            message,
            purchase,
            scope,
            self._hybrid,
        )
        retrieved: RetrievalResult = pctx.retrieved
        version = pctx.version
        trace["scope"] = {
            "types": list(scope.types),
            "included": scope.included,
            "excluded": scope.excluded,
        }
        trace["retrieval"] = {
            "eligible": retrieved.trace.eligible,
            "fts": list(retrieved.trace.fts),
            "vec": list(retrieved.trace.vec),
            "clauses": [
                {
                    "key": r.clause.chunk_key,
                    "score": round(r.score, 6),
                    "fts_rank": r.fts_rank,
                    "vec_rank": r.vec_rank,
                }
                for r in retrieved.clauses
            ],
            "incorporated": [r.clause.chunk_key for r in retrieved.incorporated],
        }
        machine.to(State.POLICY_RETRIEVED, "scoped retrieval complete")

        # ---- the determination (deterministic) -----------------------------------------------------------------
        result: OrderStatusResult = determine_order_status(inp, today, version)
        applied: list[PolicyClause] = []
        keys = result.applied_clause_keys
        if keys:
            applied = [pctx.clauses[k] for k in keys if k in pctx.clauses]
            if len(applied) != len(keys):  # the cited clauses must exist; otherwise refuse to guess
                result = OrderStatusResult(
                    OrderStatusOutcome.NEEDS_HUMAN,
                    (ReasonCode.POLICY_VERSION_NOT_IMPLEMENTED,),
                    (),
                    result.policy_version,
                    {**result.facts, "missing_clauses": ",".join(keys)},
                )
                applied = []
        det = Determination(order.id, order.order_number, result, input_state_of(inp))
        decision_id = persist_determination(conn, ctx, conv_id, det)
        trace["determination"] = {**det.as_dict(), "decision_id": str(decision_id)}
        machine.to(State.DECIDED, f"{det.outcome}: {','.join(det.reason_codes)}")
        citations = tuple(Citation(c.chunk_key, c.citation, c.text) for c in applied)

        def out(reply: str, **kw: Any) -> _Out:
            return _Out(
                reply, citations=citations, determination=det, decision_id=decision_id, **kw
            )

        if det.outcome == OrderStatusOutcome.NEEDS_HUMAN.value:
            machine.to(State.HANDED_OFF, f"needs_human: {det.reason_codes[0]}")
            return out(
                templates.status_text(det) + " " + templates.handoff_notice(),
                handoff_reason=det.reason_codes[0],
                evidence={"determination": det.as_dict(), "decision_id": str(decision_id)},
            )

        # ---- model call 2: explanation, verified against the determination -----------------------------
        try:
            exp = self._client.complete_structured(
                explanation_request(
                    det, applied, retrieved.context(), message, self._max_tokens, scenario_id
                ),
                Explanation,
                budget=budget,
            )
        except CallBudgetExceeded as exc:
            machine.to(State.HANDED_OFF, "call budget")
            return out(
                templates.status_text(det) + " " + templates.handoff_notice(),
                handoff_reason=ReasonCode.CALL_BUDGET_EXCEEDED.value,
                evidence={"determination": det.as_dict(), "error": str(exc)},
            )
        except LLMError as exc:  # the determination is still true: show it from a template
            trace["explanation"] = {
                "error": type(exc).__name__,
                "status": to_workflow_status(exc),
                "detail": exc.detail or str(exc)[:300],
            }
            machine.to(State.ANSWERED, f"explanation unavailable: {type(exc).__name__}")
            return out(
                f"{templates.status_text(det)} {templates.closing_line(det.outcome, False)}".strip(),
                degraded=True,
                answer_reason=ReasonCode.MODEL_UNAVAILABLE.value,
            )
        problems = verify_explanation(exp.parsed, det)
        trace["explanation"] = {
            "model_outcome": exp.parsed.outcome,
            "model_cited": exp.parsed.cited_clause_keys,
            "attempts": exp.attempts,
            "problems": problems,
            "commitment_phrases": commitment_phrases(exp.parsed.explanation_text),
            "accepted": not problems,
            "llm": _llm_entry(exp.raw.trace),
        }
        if problems:  # FAIL CLOSED: the customer sees the determination, never the model's words
            machine.to(State.HANDED_OFF, "explanation disagrees with the determination")
            return out(
                templates.status_text(det) + " " + templates.handoff_notice(),
                handoff_reason=ReasonCode.EXPLANATION_MISMATCH.value,
                evidence={
                    "determination": det.as_dict(),
                    "decision_id": str(decision_id),
                    "mismatches": problems,
                    "rejected_model_output": exp.parsed.model_dump(),
                },
            )
        machine.to(State.ANSWERED, "explanation verified")
        closing = templates.closing_line(det.outcome, handoff_raised=False)
        return out(f"{exp.parsed.explanation_text.strip()} {closing}".strip())

    # ------------------------------------------------------------------------------------------
    def _scan(self, message: str) -> GuardVerdict:
        if self._guard is None:
            return GuardVerdict(available=False, error="not configured")
        return self._guard.scan(message)

    @staticmethod
    def _to_handoff(machine: StateMachine, reason: ReasonCode, detail: str) -> _Out:
        machine.to(State.HANDED_OFF, reason.value)
        return _Out(
            templates.handoff_notice(), handoff_reason=reason.value, evidence={"detail": detail}
        )

    def _fail_handoff(
        self, machine: StateMachine, reason: ReasonCode, detail: str, message: str
    ) -> _Out:
        if not machine.terminal:
            machine.to(State.HANDED_OFF, reason.value)
        return _Out(
            templates.handoff_notice(), handoff_reason=reason.value, evidence={"detail": detail}
        )

    # ---- persistence of the conversation record -----------------------------------------------------
    def _open_conversation(
        self, conn: Connection, ctx: CustomerContext, conversation_id: UUID | None
    ) -> tuple[UUID, int, int]:
        if conversation_id is None:
            new_id: UUID = conn.execute(
                text("INSERT INTO conversations (customer_id) VALUES (:c) RETURNING id"),
                {"c": ctx.customer_id},
            ).scalar_one()
            return new_id, 1, 0
        row = conn.execute(
            text(
                "SELECT c.tool_calls_used, (SELECT count(*) FROM turn_traces t WHERE t.conversation_id = c.id) AS turns"
                " FROM conversations c WHERE c.id = :i AND c.customer_id = :c"
            ),
            {"i": conversation_id, "c": ctx.customer_id},
        ).one_or_none()
        if row is None:
            raise ConversationNotFound(str(conversation_id))
        return conversation_id, int(row.turns) + 1, int(row.tool_calls_used)

    @staticmethod
    def _store_message(
        conn: Connection, ctx: CustomerContext, conv_id: UUID, role: str, content: str, verdict: str
    ) -> None:
        conn.execute(
            text(
                "INSERT INTO messages (conversation_id, customer_id, role, content, injection_verdict)"
                " VALUES (:conv, :c, :r, :t, :v)"
            ),
            {"conv": conv_id, "c": ctx.customer_id, "r": role, "t": content, "v": verdict},
        )

    @staticmethod
    def _record_handoff(
        conn: Connection,
        ctx: CustomerContext,
        conv_id: UUID,
        kind: str,
        reason: str,
        evidence: dict[str, Any],
    ) -> UUID:
        import json

        hid: UUID = conn.execute(
            text(
                "INSERT INTO handoffs (conversation_id, customer_id, kind, reason_code, evidence)"
                " VALUES (:conv, :c, :k, :r, CAST(:e AS jsonb)) RETURNING id"
            ),
            {
                "conv": conv_id,
                "c": ctx.customer_id,
                "k": kind,
                "r": reason,
                "e": json.dumps(evidence, default=str),
            },
        ).scalar_one()
        return hid

    @staticmethod
    def _finalize(
        conn: Connection,
        ctx: CustomerContext,
        conv_id: UUID,
        turn_no: int,
        machine: StateMachine,
        tools: ToolRegistry,
        budget: CallBudget,
        trace: dict[str, Any],
    ) -> None:
        import json

        conn.execute(
            text(
                "UPDATE conversations SET state = :s, tool_calls_used = tool_calls_used + :n WHERE id = :i"
            ),
            {"s": machine.state.value, "n": tools.used, "i": conv_id},
        )
        for t in machine.history:  # every transition is audit-logged (SPEC 3)
            conn.execute(
                text(
                    "INSERT INTO audit_log (actor_type, customer_id, event, entity_type, entity_id, before, after)"
                    " VALUES ('system', :c, 'state_transition', 'conversation', :e,"
                    " CAST(:b AS jsonb), CAST(:a AS jsonb))"
                ),
                {
                    "c": ctx.customer_id,
                    "e": str(conv_id),
                    "b": json.dumps({"state": t.from_state.value}),
                    "a": json.dumps(
                        {"state": t.to_state.value, "reason": t.reason, "turn": turn_no}
                    ),
                },
            )
        conn.execute(
            text(
                "INSERT INTO turn_traces (conversation_id, customer_id, turn_no, final_state, tool_calls,"
                " model_calls, trace) VALUES (:conv, :c, :n, :s, :t, :m, CAST(:tr AS jsonb))"
            ),
            {
                "conv": conv_id,
                "c": ctx.customer_id,
                "n": turn_no,
                "s": machine.state.value,
                "t": tools.used,
                "m": budget.used,
                "tr": json.dumps(trace, default=str),
            },
        )
