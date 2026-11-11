"""Retrieval re-measurement, round 2 (task 7D): a tuned full-text leg and structured scoping.

    uv run python -m tests.retrieval_eval2        # writes docs/retrieval/recall_v2.json

PRE-REGISTERED PROTOCOL (written before this module was run):

  Sets. Selection = the 16 queries k was first chosen on. Held-out = the 32 queries of round 1.
  THIS IS THE LAST TUNING PASS AGAINST THE EXISTING HELD-OUT SET: it has now informed one
  configuration decision (below) and is weakly contaminated. The headline retrieval figure in the
  M3 report must come from a set generated fresh at that point. No third cycle is run on it.
  Held-out is REPORTED at every stage; it never chooses anything.

  Stage 1, lexical configuration (R1). Four configurations, fixed in advance (LEXICAL_GRID):
    A  websearch AND, ts_rank_cd, title weight A
    B  AND then OR fill, ts_rank_cd, title weight A
    C  OR, ts_rank_cd, title weight A
    D  OR, ts_rank, no weights            (the original, untuned leg: the baseline)
  Chosen on the SELECTION set, under the round-1 scope (routing by intent only), by full-text-only
  full recall at k=7; ties go to the first listed (A, B, C, D).

  Stage 2, structured scoping (decision 2). The scope is narrowed by facts from live order data
  (app/retrieval/scope.py). Compared with the round-1 scope with retrieval held fixed.

  Stage 3, fusion (R2). Candidates under the final scope, with the chosen lexical configuration:
  vector-only, full-text-only, and hybrid with a full-text weight of 0.25, 0.5 or 1.0 (vector 1.0).
  Hybrid is adopted ONLY IF its best weight strictly beats vector-only on selection full recall at
  k=7; a tie goes to vector-only (the simpler system). The weight, if hybrid wins, is the best on
  selection (ties to the larger).

  Stage 4, k (R3). The smallest k in 3..10 at which the adopted system reaches full recall on at
  least 15 of the 16 selection queries; if none, k is left at its previous value (7).

  Diagnostics that choose NOTHING: a numeric-token probe set (does a lexical leg win when the query
  contains the exact token?), a gift-card scoping-necessity set, miss attribution, scope coverage.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from math import comb
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, text

from app.auth.context import CustomerContext
from app.clock import FixedClock
from app.domain.types import PurchaseDate
from app.retrieval.embedder import Embedder
from app.retrieval.hybrid import (
    HybridConfig,
    LexicalConfig,
    ScopeFilters,
    ScopeRecord,
    _core_types,
    fts_leg,
    fuse,
    retrieval_scope_for,
    vector_leg,
)
from app.retrieval.scope import (
    NO_ORDER_CONTEXT,
    ScopeFacts,
    decide_scope,
    facts_for_order,
    facts_for_subscription,
)
from app.seed import ids
from tests.retrieval_eval import Query, all_queries

RESULTS = Path(__file__).resolve().parents[2] / "docs" / "retrieval" / "recall_v2.json"
KS = tuple(range(3, 11))
CANDIDATES = 20
SELECTION_RULE_MIN_FULL = 15
SEL_K_FOR_CHOICES = 7

LEXICAL_GRID: dict[str, LexicalConfig] = {
    "A": LexicalConfig("and", "rank_cd", True),
    "B": LexicalConfig("and_then_or", "rank_cd", True),
    "C": LexicalConfig("or", "rank_cd", True),
    "D": LexicalConfig("or", "rank", False),
}
FTS_WEIGHTS = (0.25, 0.5, 1.0)

# Which live record each case is about, so its scope facts come from real data.
CASE_SUBJECT: dict[str, tuple[str, str, str]] = {
    "refund_v1_above_ceiling_human_review": ("order", "alex", "SIM-1001"),
    "promo_discount_paid_differs_from_list": ("order", "alex", "SIM-1002"),
    "digital_addon_accessed_with_refundable_items": ("order", "alex", "SIM-1003"),
    "final_sale_item_in_order": ("order", "alex", "SIM-1004"),
    "lost_shipment_with_replacement_sent": ("order", "blair", "SIM-2001"),
    "annual_v1_inside_proration_window": (
        "subscription",
        "blair",
        "annual_v1_inside_proration_window",
    ),
    "monthly_v2_inside_cooling_off": ("subscription", "carol", "monthly_v2_inside_cooling_off"),
    "annual_v2_day10_cooling_off_beats_proration": (
        "subscription",
        "alex",
        "annual_v2_day10_cooling_off_beats_proration",
    ),
}

GIFT_CARD_SET: tuple[tuple[str, frozenset[str]], ...] = (
    (
        "I bought a gift card by mistake yesterday. Can I get my money back?",
        frozenset({"gift_cards-v2-clause-2"}),
    ),
    (
        "Refund my gift card order SIM-3002, I have not used the code.",
        frozenset({"gift_cards-v2-clause-2", "gift_cards-v2-clause-1"}),
    ),
    (
        "Is a gift card refundable after I have redeemed part of it?",
        frozenset({"gift_cards-v2-clause-1"}),
    ),
    (
        "How long after buying a gift card can I still cancel the purchase?",
        frozenset({"gift_cards-v2-clause-2"}),
    ),
)

NUMERIC_PROBES: tuple[tuple[str, str, str], ...] = (
    ("what is the auto-approve ceiling of 7500 cents", "2026-02-15", "refund-v1-clause-3"),
    ("ceiling raised to 12000 cents", "2026-04-01", "refund-v2-clause-2"),
    ("45 days refund window", "2026-04-01", "refund-v2-clause-1"),
    ("30 days from delivery refund window", "2026-02-15", "refund-v1-clause-1"),
    ("14-day cooling-off period first charge", "2026-04-01", "cancellation-v2-clause-2"),
    ("first 60 days prorated refund annual plan", "2026-02-15", "cancellation-v1-clause-3"),
    ("presumed lost 10 business days after ship date", "2026-04-01", "shipping-v1-clause-2"),
    ("7 business days standard delivery window", "2026-04-01", "shipping-v1-clause-1"),
    (
        "hazardous goods presumed lost 20 business days",
        "2026-04-01",
        "shipping_restrictions-v1-clause-3",
    ),
    (
        "unredeemed gift card refunded within 30 days of purchase",
        "2026-04-01",
        "gift_cards-v2-clause-2",
    ),
)


@dataclass(frozen=True)
class Legs:
    query: Query
    needed: frozenset[str]
    fts: list[Any]
    vec: list[Any]
    types: tuple[str, ...]
    scope_size: int
    scope: ScopeRecord | None
    core: frozenset[str]


def _ctx(name: str) -> CustomerContext:
    return CustomerContext(ids.customer_id(name))


def facts_for_case(conn: Connection, case_key: str) -> ScopeFacts:
    kind, who, ref = CASE_SUBJECT[case_key]
    clock = FixedClock()
    found = (
        facts_for_order(conn, _ctx(who), ref, clock)
        if kind == "order"
        else facts_for_subscription(conn, _ctx(who), ids.subscription_id(ref))
    )
    assert found is not None, case_key
    return found


def _eligible(
    conn: Connection, d: PurchaseDate, types: Sequence[str], filters: ScopeFilters
) -> int:
    extra = ""
    if filters.customer_only:
        extra += " AND d.audience = 'customer'"
    if filters.current_only:
        extra += " AND d.status = 'current'"
    return int(
        conn.execute(
            text(
                "SELECT count(*) FROM policy_chunks c JOIN policy_documents d ON d.id = c.document_id"  # noqa: S608
                " WHERE c.validity @> CAST(:d AS date) AND c.policy_type = ANY(CAST(:t AS text[]))"
                + extra
            ),
            {"d": d, "t": list(types)},
        ).scalar_one()
    )


class Vectors:
    def __init__(self, embedder: Embedder) -> None:
        self._e = embedder
        self._cache: dict[str, list[float]] = {}

    def get(self, q: str) -> list[float]:
        if q not in self._cache:
            self._cache[q] = [float(x) for x in self._e.embed([q])[0]]
        return self._cache[q]


def collect(
    conn: Connection,
    vectors: Vectors,
    queries: Sequence[Query],
    scope_kind: str,
    lexical: LexicalConfig,
    facts: dict[str, ScopeFacts],
) -> list[Legs]:
    """scope_kind 'S3' = round-1 scope (routing by intent only); 'S4' = structured order facts."""
    filters = ScopeFilters(True, True)
    out: list[Legs] = []
    for q in queries:
        d = PurchaseDate(q.purchase)
        record: ScopeRecord | None = None
        if scope_kind == "S3":
            types = retrieval_scope_for(q.intents, delivery_issue=q.delivery_issue)
        else:
            record = decide_scope(q.intents, facts[q.case_key])
            types = record.types
        core = frozenset(
            _core_types(
                q.intents,
                facts[q.case_key].delivery_issue if scope_kind == "S4" else q.delivery_issue,
            )
        )
        out.append(
            Legs(
                q,
                q.needed,
                fts_leg(conn, q.text, d, types, CANDIDATES, filters, lexical),
                vector_leg(conn, vectors.get(q.text), d, types, CANDIDATES, filters),
                types,
                _eligible(conn, d, types, filters),
                record,
                core,
            )
        )
    return out


MODES = {
    "fts_only": (1.0, 0.0),
    "vector_only": (0.0, 1.0),
    "hybrid_w0.25": (0.25, 1.0),
    "hybrid_w0.5": (0.5, 1.0),
    "hybrid_w1.0": (1.0, 1.0),
}


def _full(legs: Sequence[Legs], k: int, wf: float, wv: float) -> int:
    cfg = HybridConfig(k, CANDIDATES, 60, wf, wv)
    return sum(lg.needed <= {r.clause.chunk_key for r in fuse(lg.fts, lg.vec, cfg)} for lg in legs)


def _random_full(n: int, size: int, k: int) -> float:
    if k >= size:
        return 1.0
    return comb(size - n, k - n) / comb(size, k) if k >= n else 0.0


def table(
    legs: Sequence[Legs], modes: Sequence[str] = tuple(MODES), ks: Sequence[int] = KS
) -> dict[str, Any]:
    return {
        "queries": len(legs),
        "scope_sizes": sorted({lg.scope_size for lg in legs}),
        "random": {
            str(k): round(sum(_random_full(len(lg.needed), lg.scope_size, k) for lg in legs), 2)
            for k in ks
        },
        "full_recall": {m: {str(k): _full(legs, k, *MODES[m]) for k in ks} for m in modes},
    }


def noncore_slots(legs: Sequence[Legs], k: int, wf: float, wv: float) -> dict[str, int]:
    cfg = HybridConfig(k, CANDIDATES, 60, wf, wv)
    slots = off = 0
    for lg in legs:
        for r in fuse(lg.fts, lg.vec, cfg):
            slots += 1
            off += r.clause.policy_type not in lg.core
    return {"slots": slots, "non_core": off}


def measure(conn: Connection, embedder: Embedder) -> dict[str, Any]:
    queries = all_queries()
    sel = [q for q in queries if q.split == "selection"]
    held = [q for q in queries if q.split == "heldout"]
    vectors = Vectors(embedder)
    facts = {c: facts_for_case(conn, c) for c in CASE_SUBJECT}
    result: dict[str, Any] = {
        "protocol": (__doc__ or "").split("PRE-REGISTERED PROTOCOL")[1].strip()
    }

    # ---- Stage 1: lexical grid under the round-1 scope -------------------------------------------
    grid: dict[str, Any] = {}
    legs_by_cfg: dict[str, tuple[list[Legs], list[Legs]]] = {}
    for name, cfg in LEXICAL_GRID.items():
        s3_sel = collect(conn, vectors, sel, "S3", cfg, facts)
        s3_held = collect(conn, vectors, held, "S3", cfg, facts)
        legs_by_cfg[name] = (s3_sel, s3_held)
        grid[name] = {
            "config": {"form": cfg.form, "rank": cfg.rank, "title_weight": cfg.title_weight},
            "selection": table(s3_sel, ("fts_only", "vector_only", "hybrid_w1.0")),
            "heldout": table(s3_held, ("fts_only", "vector_only", "hybrid_w1.0")),
        }
    result["stage1_lexical_grid_S3"] = grid
    scores = {
        n: grid[n]["selection"]["full_recall"]["fts_only"][str(SEL_K_FOR_CHOICES)]
        for n in LEXICAL_GRID
    }
    best = max(scores.values())
    chosen_name = next(n for n in LEXICAL_GRID if scores[n] == best)  # ties: first listed
    chosen = LEXICAL_GRID[chosen_name]
    result["R1"] = {"fts_only_full_recall_k7_selection": scores, "chosen": chosen_name}

    # ---- Stage 2: structured scoping, retrieval held fixed ----------------------------------------
    s3_sel, s3_held = legs_by_cfg[chosen_name]
    s4_sel = collect(conn, vectors, sel, "S4", chosen, facts)
    s4_held = collect(conn, vectors, held, "S4", chosen, facts)
    result["stage2_scoping"] = {
        "S3_round1_scope": {"selection": table(s3_sel), "heldout": table(s3_held)},
        "S4_structured_scope": {"selection": table(s4_sel), "heldout": table(s4_held)},
        "noncore_slots_hybrid_w1.0_k7": {
            "S3_heldout": noncore_slots(s3_held, 7, 1.0, 1.0),
            "S4_heldout": noncore_slots(s4_held, 7, 1.0, 1.0),
            "S3_selection": noncore_slots(s3_sel, 7, 1.0, 1.0),
            "S4_selection": noncore_slots(s4_sel, 7, 1.0, 1.0),
        },
    }

    # ---- Stage 3: fusion (R2) ---------------------------------------------------------------------
    sel_scores = {m: _full(s4_sel, SEL_K_FOR_CHOICES, *MODES[m]) for m in MODES}
    hybrids = {m: v for m, v in sel_scores.items() if m.startswith("hybrid")}
    best_hybrid_score = max(hybrids.values())
    best_hybrid = [
        m for m in ("hybrid_w1.0", "hybrid_w0.5", "hybrid_w0.25") if hybrids[m] == best_hybrid_score
    ][0]
    adopt = best_hybrid if best_hybrid_score > sel_scores["vector_only"] else "vector_only"
    result["R2"] = {
        "selection_full_recall_k7": sel_scores,
        "best_hybrid": best_hybrid,
        "rule": "hybrid adopted only if its best weight strictly beats vector_only on selection at k=7",
        "adopted": adopt,
    }

    # ---- Stage 4: k (R3) --------------------------------------------------------------------------
    wf, wv = MODES[adopt]
    k_chosen = next((k for k in KS if _full(s4_sel, k, wf, wv) >= SELECTION_RULE_MIN_FULL), 7)
    met = any(_full(s4_sel, k, wf, wv) >= SELECTION_RULE_MIN_FULL for k in KS)
    result["R3"] = {"adopted_system": adopt, "chosen_k": k_chosen, "rule_met": met}
    result["final"] = {
        "system": adopt,
        "k": k_chosen,
        "lexical_config": chosen_name,
        "heldout_full_recall_by_k": {str(k): _full(s4_held, k, wf, wv) for k in KS},
        "selection_full_recall_by_k": {str(k): _full(s4_sel, k, wf, wv) for k in KS},
        "heldout_queries": len(held),
    }

    # ---- diagnostics (choose nothing) ---------------------------------------------------------------
    cov_ok = cov_total = 0
    attribution: dict[str, dict[str, int]] = {}
    cfg_k = HybridConfig(k_chosen, CANDIDATES, 60, wf, wv)
    for split_name, legs in (("selection", s4_sel), ("heldout", s4_held)):
        miss = {"scoping": 0, "ranking": 0, "date": 0}
        for lg in legs:
            got = {r.clause.chunk_key for r in fuse(lg.fts, lg.vec, cfg_k)}
            for key in lg.needed:
                cov_total += 1
                in_scope = conn.execute(
                    text(
                        "SELECT policy_type = ANY(CAST(:t AS text[])) AS ok, validity @> CAST(:d AS date) AS inr FROM policy_chunks WHERE chunk_key = :k"
                    ),
                    {"t": list(lg.types), "d": PurchaseDate(lg.query.purchase), "k": key},
                ).one()
                cov_ok += bool(in_scope.ok and in_scope.inr)
                if key not in got:
                    miss[
                        "scoping" if not in_scope.ok else "date" if not in_scope.inr else "ranking"
                    ] += 1
        attribution[split_name] = miss
    result["diagnostics"] = {
        "scope_coverage": {"needed_clauses_inside_scope": cov_ok, "needed_clauses": cov_total},
        "missed_clause_attribution_at_final_config": attribution,
        # Cases whose structured scope holds only 2-3 clauses (the subscription cases) retrieve
        # everything at any k >= 3. Recall on the remaining queries is the figure with signal.
        "non_trivial_queries_only": {
            "definition": "queries whose in-scope set has more than 3 clauses",
            "selection": table([lg for lg in s4_sel if lg.scope_size > 3]),
            "heldout": table([lg for lg in s4_held if lg.scope_size > 3]),
        },
        "gift_card_scoping": gift_card_check(conn, vectors, chosen, wf, wv, k_chosen),
        "numeric_probe": numeric_probe(conn, vectors, chosen, k_chosen),
    }
    return result


def gift_card_check(
    conn: Connection, vectors: Vectors, lex: LexicalConfig, wf: float, wv: float, k: int
) -> dict[str, Any]:
    """Queries whose order really contains a gift card: is the gift card policy in scope and are the
    needed clauses retrieved? And the NEGATIVE CONTROL: the same text asked about an order with no
    gift card is scoped out, and the trace says why."""
    cfg = HybridConfig(k, CANDIDATES, 60, wf, wv)
    d = PurchaseDate(__import__("datetime").date(2026, 3, 18))
    filters = ScopeFilters(True, True)
    with_gc = facts_for_order(conn, _ctx("carol"), "SIM-3002", FixedClock())
    without = facts_for_order(conn, _ctx("alex"), "SIM-1001", FixedClock())
    assert with_gc is not None and without is not None
    hit = hit_neg = 0
    reasons: set[str] = set()
    for q, needed in GIFT_CARD_SET:
        for facts, is_neg in ((with_gc, False), (without, True)):
            rec = decide_scope(["refund_request"], facts)
            fts = fts_leg(conn, q, d, rec.types, CANDIDATES, filters, lex)
            vec = vector_leg(conn, vectors.get(q), d, rec.types, CANDIDATES, filters)
            got = {r.clause.chunk_key for r in fuse(fts, vec, cfg)}
            if is_neg:
                hit_neg += needed <= got
                reasons.add(rec.excluded.get("gift_cards", ""))
            else:
                hit += needed <= got
    return {
        "queries": len(GIFT_CARD_SET),
        "order_with_gift_card_full_recall": hit,
        "same_queries_on_an_order_without_one_full_recall": hit_neg,
        "scope_trace_reason_when_excluded": sorted(reasons),
    }


def numeric_probe(
    conn: Connection, vectors: Vectors, tuned: LexicalConfig, k: int
) -> dict[str, Any]:
    """Does a lexical leg win when the query contains the exact token? Scope: a general question
    (no order context), so every customer-facing policy competes. Reports only."""
    import datetime as dt

    filters = ScopeFilters(True, True)
    rows: dict[str, dict[str, int]] = {}
    for k_ in (1, 3, k):
        rows[str(k_)] = {"fts_tuned": 0, "fts_baseline": 0, "vector": 0, "hybrid_w1.0_tuned": 0}
    for q, day, want in NUMERIC_PROBES:
        d = PurchaseDate(dt.date.fromisoformat(day))
        types = decide_scope(["policy_question"], NO_ORDER_CONTEXT).types
        tuned_fts = fts_leg(conn, q, d, types, CANDIDATES, filters, tuned)
        base_fts = fts_leg(conn, q, d, types, CANDIDATES, filters, LEXICAL_GRID["D"])
        vec = vector_leg(conn, vectors.get(q), d, types, CANDIDATES, filters)
        for k_ in (1, 3, k):
            cfg = HybridConfig(k_, CANDIDATES, 60, 1.0, 1.0)
            rows[str(k_)]["fts_tuned"] += want in [c.chunk_key for c in tuned_fts[:k_]]
            rows[str(k_)]["fts_baseline"] += want in [c.chunk_key for c in base_fts[:k_]]
            rows[str(k_)]["vector"] += want in [c.chunk_key for c in vec[:k_]]
            rows[str(k_)]["hybrid_w1.0_tuned"] += want in {
                r.clause.chunk_key for r in fuse(tuned_fts, vec, cfg)
            }
    return {"queries": len(NUMERIC_PROBES), "found_in_top_k": rows}


def main() -> None:
    from sqlalchemy import create_engine

    from app.config import get_settings
    from app.retrieval.embedder import OnnxBgeEmbedder

    engine = create_engine(get_settings().database_url)
    with engine.connect() as conn:
        result = measure(conn, OnnxBgeEmbedder())
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {RESULTS}")
    print(
        "R1:",
        result["R1"]["chosen"],
        "| R2:",
        result["R2"]["adopted"],
        "| k:",
        result["R3"]["chosen_k"],
    )


if __name__ == "__main__":
    main()
