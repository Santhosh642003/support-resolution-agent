"""Hybrid retrieval: PostgreSQL full-text search + pgvector similarity, fused by reciprocal rank
fusion (RRF). No LLM involvement.

THE DATE FILTER COMES FIRST. Both retrieval legs query only chunks whose `validity` contains the
purchase date (and whose policy type is in scope), so an out-of-range clause is never ranked by
either leg, never reaches fusion, and therefore cannot appear in the results, whatever the query
says. The filter is part of each leg's WHERE clause, not a step applied to the fused list.

The date passed is ALWAYS the purchase date (the subscription start date for cancellation); it
selects the policy version. The refund window anchor (delivery / presumed-lost date) is a
different date and never goes here (`PurchaseDate`, not `WindowAnchorDate`).

TWO WAYS A CLAUSE CAN BE RETURNED, deliberately kept apart (a future reader will want to
collapse them; do not):

  * DIRECT: the clause's own validity range contains the purchase date. This is what the legs
    return and what `RetrievalResult.clauses` holds. The validity range decides which VERSION
    IS IN EFFECT.
  * INCORPORATED: an earlier-version clause that an in-effect clause explicitly names
    (`policy_incorporations`, declared in the document's front matter), followed ONE hop after
    fusion. It arrives because the in-effect clause pulled it in, NOT because its own validity
    matched the date (it usually did not: it is a v1 clause for a v2 purchase). It is held in
    `RetrievalResult.incorporated`, carries `incorporated_by`, and its citation always names the
    clause that incorporated it. A bare citation to the earlier version would be wrong: that
    version is not in effect for this purchase.

Fusion weights, the fused list length `k`, the per-leg candidate depth and the RRF constant are
configuration (Settings.retrieval_*), not constants in this module. Incorporated clauses are
additional to `k`.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from sqlalchemy import Connection, text

from app.config import Settings
from app.domain.types import PurchaseDate
from app.retrieval.embedder import Embedder
from app.retrieval.ingest import vector_literal
from app.retrieval.policy_store import PolicyClause

# The policy types an intent NEEDS. Deterministic routing, decided in code, not by a model.
POLICY_TYPES_BY_INTENT: dict[str, tuple[str, ...]] = {
    "order_status": ("shipping",),
    "refund_request": ("refund", "shipping"),  # shipping only if delivery_issue (see below)
    "cancel_subscription": ("cancellation",),
    "policy_question": ("shipping", "refund", "cancellation"),
}

# Customer-facing policies that a message about the core type could plausibly touch. The message
# does not reveal which one applies ("my gift card order", "it arrived broken": warranty or
# refund?), so retrieval for a core type is scoped to the core type PLUS these, and ranking has
# to tell them apart. Audience and status are filtered separately (ScopeFilters).
RELATED_TYPES: dict[str, tuple[str, ...]] = {
    "refund": ("gift_cards", "warranty"),
    "shipping": ("shipping_restrictions",),
    "cancellation": ("gift_cards",),
}

_ORDER = (
    "shipping",
    "refund",
    "cancellation",
    "gift_cards",
    "shipping_restrictions",
    "warranty",
    "merchant_returns",
    "faq_archive",
)


def _core_types(intents: Sequence[str], delivery_issue: bool) -> set[str]:
    wanted = {t for i in intents for t in POLICY_TYPES_BY_INTENT.get(i, ())}
    only_a_refund = "refund_request" in intents and not (
        {"order_status", "policy_question"} & set(intents)
    )
    if only_a_refund and not delivery_issue:
        wanted.discard("shipping")
    return wanted


def policy_types_for(intents: Sequence[str], *, delivery_issue: bool = False) -> tuple[str, ...]:
    """The policy types the intents NEED, in a stable order (a combined request is a union).

    Refund requests include the shipping policy only for delivery problems: otherwise the
    shipping clauses are noise in the prompt. `delivery_issue` is a structured fact (the reason
    class from extraction, or a late/lost shipment found in the live data), never inferred here
    from free text.
    """
    wanted = _core_types(intents, delivery_issue)
    return tuple(t for t in _ORDER if t in wanted)


def retrieval_scope_for(intents: Sequence[str], *, delivery_issue: bool = False) -> tuple[str, ...]:
    """The core types plus the related customer-facing types a message could plausibly touch."""
    core = _core_types(intents, delivery_issue)
    wanted = set(core) | {r for t in core for r in RELATED_TYPES.get(t, ())}
    return tuple(t for t in _ORDER if t in wanted)


@dataclass(frozen=True)
class ScopeFilters:
    """Metadata filters applied inside each leg, beside the date filter. Both default ON: a
    seller-facing policy does not apply to customers, and an archived document is superseded.
    (Switching them off exists to MEASURE what they are worth, not to be used.)"""

    customer_only: bool = True  # documents.audience = 'customer'
    current_only: bool = True  # documents.status = 'current'


DEFAULT_FILTERS = ScopeFilters()  # frozen: safe as a default argument


@dataclass(frozen=True)
class HybridConfig:
    k: int  # direct clauses returned
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


def _short(c: PolicyClause) -> str:
    return f"v{c.version} clause {c.clause_number}"


@dataclass(frozen=True)
class RetrievedClause:
    clause: PolicyClause
    score: float
    fts_rank: int | None  # 1-based rank in the full-text leg, None if that leg did not return it
    vec_rank: int | None
    # Set ONLY on an incorporated clause: the in-effect clause that pulled it in.
    incorporated_by: PolicyClause | None = None
    # Set on a DIRECT clause: the earlier-version clauses it incorporates (one hop).
    incorporates: tuple[PolicyClause, ...] = ()

    @property
    def is_incorporated(self) -> bool:
        return self.incorporated_by is not None

    @property
    def citation(self) -> str:
        """Provenance-preserving citation. Never a bare citation to an earlier version."""
        if self.incorporated_by is not None:
            return f"{self.clause.citation}, as incorporated by {self.incorporated_by.citation}"
        if self.incorporates:
            return f"{self.clause.citation}, incorporating " + ", ".join(
                _short(t) for t in self.incorporates
            )
        return self.clause.citation


@dataclass(frozen=True)
class RetrievalTrace:
    """What each leg returned BEFORE fusion (for logs, and for the date-filter test)."""

    eligible: int  # chunks in scope for this purchase date and policy types
    fts: tuple[str, ...] = ()
    vec: tuple[str, ...] = ()


@dataclass(frozen=True)
class RetrievalResult:
    clauses: tuple[RetrievedClause, ...]  # DIRECT: in effect on the purchase date, at most k
    incorporated: tuple[RetrievedClause, ...] = ()  # pulled in by a direct clause, one hop
    trace: RetrievalTrace = field(default_factory=lambda: RetrievalTrace(0))

    def context(self) -> tuple[RetrievedClause, ...]:
        """Everything to show the explainer: direct clauses, then what they incorporate."""
        return self.clauses + self.incorporated

    def citations(self) -> list[str]:
        return [r.citation for r in self.clauses]


_COLUMNS = (
    "c.chunk_key, c.policy_type, c.version, c.clause_number, c.text,"
    " lower(c.validity) AS valid_from, upper(c.validity) AS valid_to"
)
_FROM = "policy_chunks c JOIN policy_documents d ON d.id = c.document_id"


def _scope(filters: ScopeFilters) -> str:
    """The DIRECT scope: the date filter, the policy-type scope, and the metadata filters. It is
    part of each leg's WHERE clause, so nothing outside it is ever ranked."""
    parts = [
        "c.validity @> CAST(:d AS date)",
        "c.policy_type = ANY(CAST(:types AS text[]))",
    ]
    if filters.customer_only:
        parts.append("d.audience = 'customer'")
    if filters.current_only:
        parts.append("d.status = 'current'")
    return " AND ".join(parts)


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
    conn: Connection,
    query: str,
    d: PurchaseDate,
    types: Sequence[str],
    n: int,
    filters: ScopeFilters = DEFAULT_FILTERS,
) -> list[PolicyClause]:
    """Full-text leg, date-filtered. Terms are OR-ed: a natural-language message must not need
    every one of its words to appear in a clause (websearch/plain semantics are AND)."""
    rows = conn.execute(
        text(
            "WITH q AS (SELECT replace(plainto_tsquery('english', :q)::text, '&', '|')::tsquery"  # noqa: S608
            f" AS tsq) SELECT {_COLUMNS} FROM {_FROM}, q"  # (constants only, no input)
            f" WHERE {_scope(filters)} AND c.tsv @@ q.tsq"
            " ORDER BY ts_rank(c.tsv, q.tsq) DESC, c.chunk_key LIMIT :n"
        ),
        {"q": query, "d": d, "types": list(types), "n": n},
    )
    return [_clause(r) for r in rows]


def vector_leg(
    conn: Connection,
    qvec: Sequence[float],
    d: PurchaseDate,
    types: Sequence[str],
    n: int,
    filters: ScopeFilters = DEFAULT_FILTERS,
) -> list[PolicyClause]:
    """Vector leg (cosine distance), date-filtered. The iterative scan keeps a selective filter
    from silently under-returning if the planner uses the HNSW index."""
    conn.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
    rows = conn.execute(
        text(
            f"SELECT {_COLUMNS} FROM {_FROM}"  # noqa: S608 (constants only)
            f" WHERE {_scope(filters)}"
            " ORDER BY c.embedding <=> CAST(:v AS vector), c.chunk_key LIMIT :n"
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


def follow_incorporations(
    conn: Connection, direct: Sequence[RetrievedClause]
) -> tuple[list[RetrievedClause], list[RetrievedClause]]:
    """ONE hop from the in-effect (direct) clauses to the earlier-version clauses they incorporate.

    Deliberately has NO date filter: the targets are earlier versions whose validity does not
    contain the purchase date. They are returned only because a direct clause names them. The
    database guarantees the links are same-policy, earlier-version and never chained.

    Returns (direct clauses annotated with what they incorporate, the incorporated clauses).
    """
    if not direct:
        return [], []
    rows = conn.execute(
        text(
            f"SELECT i.source_chunk_key AS via, {_COLUMNS}"  # noqa: S608 (constants only)
            " FROM policy_incorporations i JOIN policy_chunks c"
            " ON c.chunk_key = i.target_chunk_key"
            " WHERE i.source_chunk_key = ANY(CAST(:k AS text[]))"
            " ORDER BY i.source_chunk_key, c.clause_number"
        ),
        {"k": [r.clause.chunk_key for r in direct]},
    )
    by_source: dict[str, list[PolicyClause]] = {}
    for row in rows:
        by_source.setdefault(row.via, []).append(_clause(row))
    annotated = [
        replace(r, incorporates=tuple(by_source.get(r.clause.chunk_key, ()))) for r in direct
    ]
    pulled: dict[str, RetrievedClause] = {}
    for r in direct:
        for target in by_source.get(r.clause.chunk_key, ()):
            pulled.setdefault(
                target.chunk_key,
                RetrievedClause(target, 0.0, None, None, incorporated_by=r.clause),
            )
    return annotated, list(pulled.values())


def retrieve(
    conn: Connection,
    embedder: Embedder,
    query: str,
    purchase_date: PurchaseDate,
    policy_types: Sequence[str],
    cfg: HybridConfig,
    filters: ScopeFilters = DEFAULT_FILTERS,
    *,
    follow: bool = True,
) -> RetrievalResult:
    """Date-filtered hybrid retrieval. Returns at most cfg.k DIRECT clauses of the versions in
    effect on `purchase_date`, never from another version; plus, if `follow`, the earlier-version
    clauses those direct clauses explicitly incorporate (one hop, flagged, never silent)."""
    if not policy_types:
        return RetrievalResult(())
    eligible = conn.execute(
        text(f"SELECT count(*) FROM {_FROM} WHERE {_scope(filters)}"),  # noqa: S608
        {"d": purchase_date, "types": list(policy_types)},
    ).scalar_one()
    fts = fts_leg(conn, query, purchase_date, policy_types, cfg.candidates_per_leg, filters)
    qvec = embedder.embed([query])[0]  # no query-instruction prefix (SPEC 14)
    vec = vector_leg(conn, qvec, purchase_date, policy_types, cfg.candidates_per_leg, filters)
    direct = fuse(fts, vec, cfg)
    incorporated: list[RetrievedClause] = []
    if follow:
        direct, incorporated = follow_incorporations(conn, direct)
    return RetrievalResult(
        tuple(direct),
        tuple(incorporated),
        RetrievalTrace(
            int(eligible),
            tuple(c.chunk_key for c in fts),
            tuple(c.chunk_key for c in vec),
        ),
    )
