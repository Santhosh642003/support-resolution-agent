"""Retrieval recall measurement (task 7C). Used by tests/db/test_retrieval_recall.py and runnable as

    uv run python -m tests.retrieval_eval        # (re)writes docs/retrieval/recall.json

PROTOCOL (fixed before the held-out queries were run):
  * selection set = the 16 queries k was previously chosen on (tests/fixtures/retrieval_cases.py);
    held-out set = 32 new queries (tests/fixtures/retrieval_heldout.py), never used to choose
    anything. Both are reported separately.
  * k is chosen on the SELECTION set only: the smallest k in 3..10 at which hybrid retrieves every
    needed clause for at least 15 of its 16 queries. If none qualifies, k is left unchanged.
  * "full recall" = every needed DIRECT clause is in the top k. "clause recall" = the fraction of
    needed clauses found. The random baseline is the expectation of picking k clauses uniformly
    from the in-scope set (same date filter, same scope).
  * Scopes: S0 production (routed types + customer/current filters); S2 all customer-facing
    current types (metadata filters, no routing); S1 all types with no metadata filters (the date
    filter alone).
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from math import comb
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, text

from app.domain.types import PurchaseDate
from app.retrieval.embedder import Embedder
from app.retrieval.hybrid import (
    HybridConfig,
    ScopeFilters,
    follow_incorporations,
    fts_leg,
    fuse,
    retrieval_scope_for,
    vector_leg,
)
from app.seed.definitions import ORDERS, SUBSCRIPTIONS
from tests.fixtures.retrieval_cases import CASES
from tests.fixtures.retrieval_heldout import HELDOUT_QUERIES

RESULTS = Path(__file__).resolve().parents[2] / "docs" / "retrieval" / "recall.json"
KS = tuple(range(3, 11))
CANDIDATES = 20
ALL_TYPES = (
    "shipping",
    "refund",
    "cancellation",
    "gift_cards",
    "shipping_restrictions",
    "warranty",
    "merchant_returns",
    "faq_archive",
)
ALL_CUSTOMER_TYPES = tuple(t for t in ALL_TYPES if t not in ("merchant_returns", "faq_archive"))
SELECTION_RULE_MIN_FULL = 15  # of 16

# The rule text a correct EXPLANATION needs that lives in an earlier version and arrives through an
# incorporation link when the in-effect clause is retrieved.
NEEDED_VIA_INCORPORATION: dict[str, frozenset[str]] = {
    "digital_addon_accessed_with_refundable_items": frozenset({"refund-v1-clause-5"}),
    "final_sale_item_in_order": frozenset({"refund-v1-clause-6"}),
    "annual_v2_day10_cooling_off_beats_proration": frozenset({"cancellation-v1-clause-3"}),
}

ORDER_DATE = {o.key: o.placed_at.date() for o in ORDERS}
SUB_DATE = {s.key: s.started_at.date() for s in SUBSCRIPTIONS}


@dataclass(frozen=True)
class Query:
    case_key: str
    text: str
    needed: frozenset[str]
    intents: tuple[str, ...]
    delivery_issue: bool
    purchase: date
    split: str  # 'selection' | 'heldout'


def all_queries() -> list[Query]:
    out: list[Query] = []
    for c in CASES:
        purchase = ORDER_DATE.get(c.case_key) or SUB_DATE[c.case_key]
        for q in c.queries:
            out.append(
                Query(c.case_key, q, c.needed, c.intents, c.delivery_issue, purchase, "selection")
            )
        for q in HELDOUT_QUERIES[c.case_key]:
            out.append(
                Query(c.case_key, q, c.needed, c.intents, c.delivery_issue, purchase, "heldout")
            )
    return out


@dataclass(frozen=True)
class Legs:
    query: Query
    fts: list[Any]
    vec: list[Any]
    scope_size: int


def collect(
    conn: Connection, embedder: Embedder, queries: Sequence[Query], scope: str
) -> list[Legs]:
    """Run both legs for every query under one scope. The date filter is in every leg."""
    filters = ScopeFilters(False, False) if scope == "S1" else ScopeFilters(True, True)
    vectors = embedder.embed([q.text for q in queries])
    out: list[Legs] = []
    for q, v in zip(queries, vectors, strict=True):
        if scope == "S0":
            types = retrieval_scope_for(q.intents, delivery_issue=q.delivery_issue)
        elif scope == "S2":
            types = ALL_CUSTOMER_TYPES
        else:
            types = ALL_TYPES
        d = PurchaseDate(q.purchase)
        size = conn.execute(
            text(
                "SELECT count(*) FROM policy_chunks c JOIN policy_documents d ON d.id = c.document_id"  # noqa: S608
                " WHERE c.validity @> CAST(:d AS date) AND c.policy_type = ANY(CAST(:t AS text[]))"
                + ("" if scope == "S1" else " AND d.audience = 'customer' AND d.status = 'current'")
            ),
            {"d": d, "t": list(types)},
        ).scalar_one()
        out.append(
            Legs(
                q,
                fts_leg(conn, q.text, d, types, CANDIDATES, filters),
                vector_leg(conn, v, d, types, CANDIDATES, filters),
                int(size),
            )
        )
    return out


MODES = {"hybrid": (1.0, 1.0), "fts_only": (1.0, 0.0), "vector_only": (0.0, 1.0)}


def _random_full(n: int, size: int, k: int) -> float:
    if k >= size:
        return 1.0
    return comb(size - n, k - n) / comb(size, k) if k >= n else 0.0


def recall_table(legs: Sequence[Legs], ks: Sequence[int] = KS) -> dict[str, Any]:
    table: dict[str, Any] = {
        "queries": len(legs),
        "scope_sizes": sorted({lg.scope_size for lg in legs}),
    }
    table["random"] = {
        str(k): {
            "full": round(
                sum(_random_full(len(lg.query.needed), lg.scope_size, k) for lg in legs), 2
            ),
            "clause": round(
                sum(min(1.0, k / lg.scope_size) * len(lg.query.needed) for lg in legs), 2
            ),
        }
        for k in ks
    }
    modes: dict[str, Any] = {}
    for name, (wf, wv) in MODES.items():
        per_k: dict[str, Any] = {}
        for k in ks:
            cfg = HybridConfig(k, CANDIDATES, 60, wf, wv)
            full = hit = 0
            total = sum(len(lg.query.needed) for lg in legs)
            for lg in legs:
                got = {r.clause.chunk_key for r in fuse(lg.fts, lg.vec, cfg)}
                found = len(lg.query.needed & got)
                hit += found
                full += found == len(lg.query.needed)
            per_k[str(k)] = {"full": full, "clause": [hit, total]}
        modes[name] = per_k
    table["modes"] = modes
    return table


def choose_k(selection: dict[str, Any], default: int) -> int:
    for k in range(3, 11):
        if selection["modes"]["hybrid"][str(k)]["full"] >= SELECTION_RULE_MIN_FULL:
            return k
    return default


def incorporation_completeness(conn: Connection, legs: Sequence[Legs], k: int) -> dict[str, int]:
    """For queries whose explanation needs an earlier-version rule text: is it present, through
    the incorporation link, whenever the in-effect clause was retrieved?"""
    cfg = HybridConfig(k, CANDIDATES, 60, 1.0, 1.0)
    relevant = source_found = text_present = 0
    for lg in legs:
        want = NEEDED_VIA_INCORPORATION.get(lg.query.case_key)
        if not want:
            continue
        relevant += 1
        direct = fuse(lg.fts, lg.vec, cfg)
        if not lg.query.needed <= {r.clause.chunk_key for r in direct}:
            continue
        source_found += 1
        _, inc = follow_incorporations(conn, direct)
        text_present += want <= {r.clause.chunk_key for r in inc}
    return {
        "queries": relevant,
        "in_effect_clauses_retrieved": source_found,
        "rule_text_present": text_present,
    }


WEIGHT_GRID = (0.0, 0.25, 0.5, 0.75, 1.0)  # weight of the full-text leg; the vector leg is 1.0
# Secondary experiment protocol: the FTS weight is chosen on the SELECTION set at the k above
# (highest full recall; ties go to the LARGER weight, so hybrid is kept unless it is worse), then
# reported on held-out. Held-out never chooses anything.


def weight_sweep(sel: Sequence[Legs], held: Sequence[Legs], k: int) -> dict[str, Any]:
    def full(legs: Sequence[Legs], w: float) -> int:
        cfg = HybridConfig(k, CANDIDATES, 60, w, 1.0)
        return sum(
            lg.query.needed <= {r.clause.chunk_key for r in fuse(lg.fts, lg.vec, cfg)}
            for lg in legs
        )

    rows = {str(w): {"selection": full(sel, w), "heldout": full(held, w)} for w in WEIGHT_GRID}
    best = max(rows[str(w)]["selection"] for w in WEIGHT_GRID)
    chosen = max(w for w in WEIGHT_GRID if rows[str(w)]["selection"] == best)
    return {
        "k": k,
        "selection_queries": len(sel),
        "heldout_queries": len(held),
        "by_fts_weight": rows,
        "chosen_fts_weight_on_selection": chosen,
        "heldout_at_chosen": rows[str(chosen)]["heldout"],
        "heldout_at_equal_weights": rows["1.0"]["heldout"],
    }


def miss_analysis(legs: Sequence[Legs], k: int) -> dict[str, Any]:
    """Which needed clauses are missed most, and what takes their slots (hybrid and vector-only)."""
    out: dict[str, Any] = {}
    for name, (wf, wv) in (("hybrid", MODES["hybrid"]), ("vector_only", MODES["vector_only"])):
        cfg = HybridConfig(k, CANDIDATES, 60, wf, wv)
        missed: dict[str, int] = {}
        asked: dict[str, int] = {}
        intruders: dict[str, int] = {}
        slots = off_core = 0
        for lg in legs:
            top = fuse(lg.fts, lg.vec, cfg)
            got = {r.clause.chunk_key for r in top}
            core = set(
                retrieval_scope_for(lg.query.intents, delivery_issue=lg.query.delivery_issue)
            )
            core_only = {t for t in core if t in ("refund", "cancellation", "shipping")}
            for key in lg.query.needed:
                asked[key] = asked.get(key, 0) + 1
                missed[key] = missed.get(key, 0) + (key not in got)
            for r in top:
                slots += 1
                if r.clause.policy_type not in core_only:
                    off_core += 1
                    intruders[r.clause.policy_type] = intruders.get(r.clause.policy_type, 0) + 1
        out[name] = {
            "missed_by_clause": {
                k_: f"{missed[k_]}/{asked[k_]}"
                for k_ in sorted(asked, key=lambda x: -missed[x])
                if missed[k_]
            },
            "slots": slots,
            "slots_taken_by_non_core_policy_types": off_core,
            "non_core_types": dict(sorted(intruders.items(), key=lambda kv: -kv[1])),
        }
    return out


def measure(conn: Connection, embedder: Embedder, default_k: int) -> dict[str, Any]:
    queries = all_queries()
    sel = [q for q in queries if q.split == "selection"]
    held = [q for q in queries if q.split == "heldout"]
    result: dict[str, Any] = {
        "protocol": __doc__.split("PROTOCOL")[1].split('"""')[0].strip() if __doc__ else ""
    }
    s0_sel = collect(conn, embedder, sel, "S0")
    s0_held = collect(conn, embedder, held, "S0")
    result["S0_selection"] = recall_table(s0_sel)
    result["S0_heldout"] = recall_table(s0_held)
    k = choose_k(result["S0_selection"], default_k)
    result["chosen_k"] = k
    result["rule"] = (
        f"smallest k in 3..10 with hybrid full recall >= {SELECTION_RULE_MIN_FULL}/16 on selection"
    )
    result["incorporation_completeness_at_k"] = {
        "selection": incorporation_completeness(conn, s0_sel, k),
        "heldout": incorporation_completeness(conn, s0_held, k),
    }
    result["weight_sweep"] = weight_sweep(s0_sel, s0_held, k)
    result["miss_analysis_heldout"] = miss_analysis(s0_held, k)
    result["miss_analysis_selection"] = miss_analysis(s0_sel, k)
    cw = result["weight_sweep"]["chosen_fts_weight_on_selection"]
    chosen_cfg = {
        str(kk): sum(
            lg.query.needed
            <= {
                r.clause.chunk_key
                for r in fuse(lg.fts, lg.vec, HybridConfig(kk, CANDIDATES, 60, cw, 1.0))
            }
            for lg in s0_held
        )
        for kk in KS
    }
    result["heldout_full_recall_by_k_at_chosen_weight"] = {"fts_weight": cw, "by_k": chosen_cfg}
    for scope in ("S2", "S1"):
        result[f"{scope}_selection"] = recall_table(collect(conn, embedder, sel, scope), (k,))
        result[f"{scope}_heldout"] = recall_table(collect(conn, embedder, held, scope), (k,))
    return result


def main() -> None:
    from sqlalchemy import create_engine

    from app.config import get_settings
    from app.retrieval.embedder import OnnxBgeEmbedder

    settings = get_settings()
    engine = create_engine(settings.database_url)
    with engine.connect() as conn:
        result = measure(conn, OnnxBgeEmbedder(), settings.retrieval_k)
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {RESULTS}; chosen k = {result['chosen_k']}")


if __name__ == "__main__":
    main()
