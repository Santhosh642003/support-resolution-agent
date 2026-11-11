"""Hybrid retrieval: PostgreSQL full-text search + pgvector similarity, fused by reciprocal rank
fusion (RRF). No LLM involvement.

THE DATE FILTER COMES FIRST. Both retrieval legs query only chunks whose `validity` contains the
purchase date (and whose policy type is in scope), so an out-of-range clause is never ranked by
either leg, never reaches fusion, and therefore cannot appear in the results, whatever the query
says. The filter is part of each leg's WHERE clause, not a step applied to the fused list.

The date passed is ALWAYS the purchase date (the subscription start date for cancellation); it
selects the policy version. The refund window anchor (delivery / presumed-lost date) is a
different date and never goes here (`PurchaseDate`, not `WindowAnchorDate`).

Fusion weights, the fused list length `k`, the per-leg candidate depth and the RRF constant are
configuration (Settings.retrieval_*), not constants in this module.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import Connection, text

from app.config import Settings
from app.domain.types import PurchaseDate
from app.retrieval.embedder import Embedder
from app.retrieval.ingest import vector_literal
from app.retrieval.policy_store import PolicyClause

# Which policy types an intent needs. Deterministic routing, decided in code, not by a model.
POLICY_TYPES_BY_INTENT: dict[str, tuple[str, ...]] = {
    "order_status": ("shipping",),
    "refund_request": ("refund", "shipping"),  # shipping only if delivery_issue (see below)
    "cancel_subscription": ("cancellation",),
    "policy_question": ("shipping", "refund", "cancellation"),
}


def policy_types_for(intents: Sequence[str], *, delivery_issue: bool = False) -> tuple[str, ...]:
    """Union of the policy types for several intents (a combined request), in a stable order.

    Refund requests need the shipping policy only when the issue is a delivery problem (the
    shipment is late or missing); otherwise the shipping clauses are noise in the prompt and
    dilute recall. `delivery_issue` is a structured fact (the reason class from extraction, or a
    late/lost shipment found in the live data), never inferred here from free text.
    """
    wanted = {t for i in intents for t in POLICY_TYPES_BY_INTENT.get(i, ())}
    only_a_refund = "refund_request" in intents and not (
        {"order_status", "policy_question"} & set(intents)
    )
    if only_a_refund and not delivery_issue:
        wanted.discard("shipping")
    return tuple(t for t in ("shipping", "refund", "cancellation") if t in wanted)


@dataclass(frozen=True)
class HybridConfig:
    k: int  # clauses returned
    candidates_per_leg: int  # depth of each leg before fusion
    rrf_k: int  # RRF constant: score = w / (rrf_k + rank)
    weight_fts: float
    weight_vec: float

    @classmethod
    def from_settings(cls, s: Settings) -> "HybridConfig":
        return cls(
            k=s.retrieval_k,
            candidates_per_leg=s.retrieval_candidates,
            rrf_k=s.retrieval_rrf_k,
            weight_fts=s.retrieval_weight_fts,
            weight_vec=s.retrieval_weight_vec,
        )


@dataclass(frozen=True)
class RetrievedClause:
    clause: PolicyClause
    score: float
    fts_rank: int | None  # 1-based rank in the full-text leg, None if that leg did not return it
    vec_rank: int | None


@dataclass(frozen=True)
class RetrievalTrace:
    """What each leg returned BEFORE fusion (for logs, and for the date-filter test)."""

    eligible: int  # chunks in scope for this purchase date and policy types
    fts: tuple[str, ...] = ()
    vec: tuple[str, ...] = ()


@dataclass(frozen=True)
class RetrievalResult:
    clauses: tuple[RetrievedClause, ...]
    trace: RetrievalTrace = field(default_factory=lambda: RetrievalTrace(0))


_COLUMNS = (
    "chunk_key, policy_type, version, clause_number, text,"
    " lower(validity) AS valid_from, upper(validity) AS valid_to"
)

# Both legs share this scope. It is the date filter, applied inside each leg.
_SCOPE = "validity @> CAST(:d AS date) AND policy_type = ANY(CAST(:types AS text[]))"


def _clause(r: object) -> PolicyClause:
    return PolicyClause(
        r.chunk_key,  # type: ignore[attr-defined]
        r.policy_type,  # type: ignore[attr-defined]
        r.version,  # type: ignore[attr-defined]
        r.clause_number,  # type: ignore[attr-defined]
        r.text,  # type: ignore[attr-defined]
        r.valid_from,  # type: ignore[attr-defined]
        r.valid_to,  # type: ignore[attr-defined]
    )


def fts_leg(
    conn: Connection, query: str, d: PurchaseDate, types: Sequence[str], n: int
) -> list[PolicyClause]:
    """Full-text leg, date-filtered. Terms are OR-ed: a natural-language message must not need
    every one of its words to appear in a clause (websearch/plain semantics are AND)."""
    rows = conn.execute(
        text(
            "WITH q AS (SELECT replace(plainto_tsquery('english', :q)::text, '&', '|')::tsquery"  # noqa: S608
            f" AS tsq) SELECT {_COLUMNS} FROM policy_chunks, q"  # (constants only, no input)
            f" WHERE {_SCOPE} AND tsv @@ q.tsq"
            " ORDER BY ts_rank(tsv, q.tsq) DESC, chunk_key LIMIT :n"
        ),
        {"q": query, "d": d, "types": list(types), "n": n},
    )
    return [_clause(r) for r in rows]


def vector_leg(
    conn: Connection, qvec: Sequence[float], d: PurchaseDate, types: Sequence[str], n: int
) -> list[PolicyClause]:
    """Vector leg (cosine distance), date-filtered. The iterative scan keeps a selective filter
    from silently under-returning if the planner uses the HNSW index."""
    conn.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
    rows = conn.execute(
        text(
            f"SELECT {_COLUMNS} FROM policy_chunks"  # noqa: S608 (constants only)
            f" WHERE {_SCOPE}"
            " ORDER BY embedding <=> CAST(:v AS vector), chunk_key LIMIT :n"
        ),
        {"v": vector_literal(qvec), "d": d, "types": list(types), "n": n},
    )
    return [_clause(r) for r in rows]


def fuse(
    fts: Sequence[PolicyClause], vec: Sequence[PolicyClause], cfg: HybridConfig
) -> list[RetrievedClause]:
    """Reciprocal rank fusion over the two ALREADY-FILTERED lists. Deterministic: ties break by
    chunk key. A clause found by one leg only still scores from that leg."""
    by_key: dict[str, PolicyClause] = {}
    f_rank: dict[str, int] = {}
    v_rank: dict[str, int] = {}
    for rank, c in enumerate(fts, start=1):
        by_key[c.chunk_key] = c
        f_rank[c.chunk_key] = rank
    for rank, c in enumerate(vec, start=1):
        by_key[c.chunk_key] = c
        v_rank[c.chunk_key] = rank
    scored: list[RetrievedClause] = []
    for key, clause in by_key.items():
        score = 0.0
        if key in f_rank:
            score += cfg.weight_fts / (cfg.rrf_k + f_rank[key])
        if key in v_rank:
            score += cfg.weight_vec / (cfg.rrf_k + v_rank[key])
        scored.append(RetrievedClause(clause, score, f_rank.get(key), v_rank.get(key)))
    scored.sort(key=lambda r: (-r.score, r.clause.chunk_key))
    return scored[: cfg.k]


def retrieve(
    conn: Connection,
    embedder: Embedder,
    query: str,
    purchase_date: PurchaseDate,
    policy_types: Sequence[str],
    cfg: HybridConfig,
) -> RetrievalResult:
    """Date-filtered hybrid retrieval. Returns at most cfg.k clauses of the versions in effect on
    `purchase_date`, never more and never from another version."""
    if not policy_types:
        return RetrievalResult(())
    eligible = conn.execute(
        text(f"SELECT count(*) FROM policy_chunks WHERE {_SCOPE}"),  # noqa: S608
        {"d": purchase_date, "types": list(policy_types)},
    ).scalar_one()
    fts = fts_leg(conn, query, purchase_date, policy_types, cfg.candidates_per_leg)
    qvec = embedder.embed([query])[0]  # no query-instruction prefix (SPEC 14)
    vec = vector_leg(conn, qvec, purchase_date, policy_types, cfg.candidates_per_leg)
    return RetrievalResult(
        tuple(fuse(fts, vec, cfg)),
        RetrievalTrace(
            int(eligible),
            tuple(c.chunk_key for c in fts),
            tuple(c.chunk_key for c in vec),
        ),
    )
