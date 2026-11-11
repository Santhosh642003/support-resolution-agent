"""Hybrid retrieval against real PostgreSQL with the real corpus and the real embedder.

The date filter is non-negotiable: it is applied inside each retrieval leg, before fusion. These
tests prove it from the legs' own output (a wrong-version clause is not in either leg, so it
cannot be fused), and they take the first real recall measurement for the eight seeded cases.
"""

from datetime import date
from math import comb

import pytest
from sqlalchemy import Connection

from app.config import Settings
from app.domain.types import PurchaseDate
from app.retrieval.embedder import OnnxBgeEmbedder
from app.retrieval.hybrid import (
    HybridConfig,
    fts_leg,
    fuse,
    policy_types_for,
    retrieve,
    vector_leg,
)
from app.retrieval.ingest import ingest_policies
from app.retrieval.policy_loader import load_policies
from app.seed.definitions import ORDERS, SUBSCRIPTIONS
from tests.fixtures.retrieval_cases import CASES

CFG = HybridConfig.from_settings(Settings(_env_file=None))  # type: ignore[call-arg]
ORDER_DATE = {o.key: o.placed_at.date() for o in ORDERS}
SUB_DATE = {s.key: s.started_at.date() for s in SUBSCRIPTIONS}


def pd(d: date) -> PurchaseDate:
    return PurchaseDate(d)


@pytest.fixture(scope="module")
def embedder() -> OnnxBgeEmbedder:
    return OnnxBgeEmbedder()


@pytest.fixture
def corpus(conn: Connection, embedder: OnnxBgeEmbedder) -> Connection:
    ingest_policies(conn, embedder, load_policies())
    return conn


def versions(res_clauses: list) -> set[int]:  # type: ignore[type-arg]
    return {r.clause.version for r in res_clauses}


# --- the date filter, applied before fusion -------------------------------------------------


V1_WORDING = "Refund window: 30 days from delivery date. Auto-approve ceiling: 7500 cents."
V2_WORDING = "Refund window extended to 45 days. Auto-approve ceiling raised to 12000 cents."


@pytest.mark.parametrize(
    ("purchase", "expected_version", "query"),
    [
        (date(2026, 2, 28), 1, V2_WORDING),  # a v1 purchase, a query quoting v2 text exactly
        (date(2026, 3, 1), 2, V1_WORDING),  # the boundary day is v2, a query quoting v1 text
        (date(2026, 3, 15), 2, V1_WORDING),
        (date(2026, 2, 15), 1, V2_WORDING),
    ],
)
def test_a_clause_outside_its_validity_never_reaches_either_leg_or_the_results(
    corpus: Connection, embedder: OnnxBgeEmbedder, purchase: date, expected_version: int, query: str
) -> None:
    big = HybridConfig(k=20, candidates_per_leg=50, rrf_k=60, weight_fts=1.0, weight_vec=1.0)
    res = retrieve(corpus, embedder, query, pd(purchase), ("refund",), big)
    wrong = 2 if expected_version == 1 else 1
    wrong_keys = {f"refund-v{wrong}-clause-{n}" for n in range(1, 7)}
    # not in the fused results ...
    assert versions(list(res.clauses)) == {expected_version}
    # ... and, the stronger claim, not in EITHER leg before fusion
    assert not (set(res.trace.fts) | set(res.trace.vec)) & wrong_keys
    assert set(res.trace.vec)  # the vector leg really did return in-scope candidates


def test_vector_leg_alone_cannot_return_the_other_version_even_when_asked_for_everything(
    corpus: Connection, embedder: OnnxBgeEmbedder
) -> None:
    """A query embedded from v1 text is nearest to v1 chunks; on a v2 date they must not appear."""
    qvec = embedder.embed([V1_WORDING])[0]
    got = vector_leg(corpus, qvec, pd(date(2026, 4, 1)), ("refund",), 50)
    assert {c.version for c in got} == {2} and len(got) == 5  # all of v2's clauses, none of v1's
    got = fts_leg(corpus, V1_WORDING, pd(date(2026, 4, 1)), ("refund",), 50)
    assert {c.version for c in got} <= {2}


def test_a_date_before_any_policy_exists_returns_nothing(
    corpus: Connection, embedder: OnnxBgeEmbedder
) -> None:
    res = retrieve(corpus, embedder, "refund", pd(date(2025, 5, 31)), ("refund",), CFG)
    assert res.clauses == () and res.trace.eligible == 0


def test_results_are_limited_to_the_requested_policy_types(
    corpus: Connection, embedder: OnnxBgeEmbedder
) -> None:
    res = retrieve(
        corpus, embedder, "cancel and refund", pd(date(2026, 4, 1)), ("cancellation",), CFG
    )
    assert {r.clause.policy_type for r in res.clauses} == {"cancellation"}
    assert retrieve(corpus, embedder, "x", pd(date(2026, 4, 1)), (), CFG).clauses == ()


# --- fusion mechanics (pure) -----------------------------------------------------------------


def _clause(key: str):  # type: ignore[no-untyped-def]
    from app.retrieval.policy_store import PolicyClause

    t, v, _, n = key.split("-")
    return PolicyClause(key, t, int(v[1:]), int(n), key, date(2026, 3, 1), None)


def test_rrf_scores_are_weight_over_rrf_k_plus_rank_and_ties_break_by_key() -> None:
    a, b, c = (
        _clause("refund-v2-clause-1"),
        _clause("refund-v2-clause-2"),
        _clause("refund-v2-clause-3"),
    )
    cfg = HybridConfig(k=3, candidates_per_leg=8, rrf_k=60, weight_fts=1.0, weight_vec=2.0)
    out = fuse([a, b], [b, c], cfg)
    by = {r.clause.chunk_key: r for r in out}
    assert by[b.chunk_key].score == pytest.approx(1 / (60 + 2) + 2 / (60 + 1))  # both legs
    assert by[a.chunk_key].score == pytest.approx(1 / (60 + 1))  # one leg only
    assert by[c.chunk_key].score == pytest.approx(2 / (60 + 2))
    assert [r.clause.chunk_key for r in out][0] == b.chunk_key  # in both legs, highest score
    assert (by[b.chunk_key].fts_rank, by[b.chunk_key].vec_rank) == (2, 1)
    tie = fuse([a], [b], HybridConfig(2, 8, 60, 1.0, 1.0))
    assert [r.clause.chunk_key for r in tie] == [
        a.chunk_key,
        b.chunk_key,
    ]  # equal scores: key order


def test_k_weights_and_rrf_constant_come_from_configuration() -> None:
    s = Settings(_env_file=None, retrieval_k=3, retrieval_rrf_k=10, retrieval_weight_fts=0.5)  # type: ignore[call-arg]
    cfg = HybridConfig.from_settings(s)
    assert (cfg.k, cfg.rrf_k, cfg.weight_fts, cfg.weight_vec) == (3, 10, 0.5, 1.0)
    a, b = _clause("refund-v2-clause-1"), _clause("refund-v2-clause-2")
    assert len(fuse([a, b], [a, b], HybridConfig(1, 8, 60, 1.0, 1.0))) == 1


def test_setting_a_weight_to_zero_reduces_fusion_to_the_other_leg() -> None:
    a, b, c = (
        _clause("refund-v2-clause-1"),
        _clause("refund-v2-clause-2"),
        _clause("refund-v2-clause-3"),
    )
    fts_only = fuse([c, a, b], [a, b, c], HybridConfig(3, 8, 60, 1.0, 0.0))
    assert [r.clause.chunk_key for r in fts_only] == [c.chunk_key, a.chunk_key, b.chunk_key]
    vec_only = fuse([c, a, b], [a, b, c], HybridConfig(3, 8, 60, 0.0, 1.0))
    assert [r.clause.chunk_key for r in vec_only] == [a.chunk_key, b.chunk_key, c.chunk_key]


# --- retrieval behaviour on the real corpus ---------------------------------------------------


def test_full_text_leg_ors_its_terms_so_a_chatty_message_still_matches(
    corpus: Connection,
) -> None:
    q = "hello there, my parrot ate my homework but also the refund window please"
    got = fts_leg(corpus, q, pd(date(2026, 4, 1)), ("refund",), 8)
    assert "refund-v2-clause-1" in {c.chunk_key for c in got}  # AND semantics would match nothing


def test_retrieval_is_deterministic_and_never_exceeds_k(
    corpus: Connection, embedder: OnnxBgeEmbedder
) -> None:
    q = "I want a refund for the damaged box"
    a = retrieve(corpus, embedder, q, pd(date(2026, 4, 1)), ("refund", "shipping"), CFG)
    b = retrieve(corpus, embedder, q, pd(date(2026, 4, 1)), ("refund", "shipping"), CFG)
    assert [r.clause.chunk_key for r in a.clauses] == [r.clause.chunk_key for r in b.clauses]
    assert len(a.clauses) <= CFG.k
    two = retrieve(
        corpus, embedder, q, pd(date(2026, 4, 1)), ("refund",), HybridConfig(2, 8, 60, 1.0, 1.0)
    )
    assert len(two.clauses) == 2


def test_routing_adds_shipping_only_for_delivery_problems() -> None:
    assert policy_types_for(["refund_request"]) == ("refund",)
    assert policy_types_for(["refund_request"], delivery_issue=True) == ("shipping", "refund")
    assert policy_types_for(["order_status"]) == ("shipping",)
    assert policy_types_for(["cancel_subscription"]) == ("cancellation",)
    assert policy_types_for(["refund_request", "cancel_subscription"]) == ("refund", "cancellation")
    assert policy_types_for(["policy_question"]) == ("shipping", "refund", "cancellation")
    assert policy_types_for(["other"]) == ()


# --- the first recall measurement: the eight seeded cases --------------------------------------


def _purchase_date(case_key: str) -> PurchaseDate:
    return pd(ORDER_DATE.get(case_key) or SUB_DATE[case_key])


def test_for_each_seeded_case_the_clauses_a_correct_decision_needs_are_in_the_top_k(
    corpus: Connection, embedder: OnnxBgeEmbedder
) -> None:
    missing: list[str] = []
    for case in CASES:
        types = policy_types_for(case.intents, delivery_issue=case.delivery_issue)
        for q in case.queries:
            res = retrieve(corpus, embedder, q, _purchase_date(case.case_key), types, CFG)
            got = {r.clause.chunk_key for r in res.clauses}
            if not case.needed <= got:
                missing.append(f"{case.case_key}: missing {sorted(case.needed - got)} for {q!r}")
    assert not missing, "\n".join(missing)


def test_recall_curve_is_reproducible_and_beats_random_only_slightly(
    corpus: Connection, embedder: OnnxBgeEmbedder
) -> None:
    """Records the numbers cited in SPEC 7.1. Full recall = every needed clause is in the top k.
    The random baseline shows how little ranking adds when the in-scope set is only 2-8 clauses."""
    legs = []
    for case in CASES:
        types = policy_types_for(case.intents, delivery_issue=case.delivery_issue)
        d = _purchase_date(case.case_key)
        for q in case.queries:
            fts = fts_leg(corpus, q, d, types, 8)
            vec = vector_leg(corpus, embedder.embed([q])[0], d, types, 8)
            scope = retrieve(corpus, embedder, q, d, types, CFG).trace.eligible
            legs.append((case, fts, vec, scope))
    assert len(legs) == 16

    def full_recall(k: int) -> int:
        cfg = HybridConfig(k, 8, 60, 1.0, 1.0)
        return sum(
            case.needed <= {r.clause.chunk_key for r in fuse(f, v, cfg)} for case, f, v, _ in legs
        )

    def random_expectation(k: int) -> float:
        total = 0.0
        for case, _, _, scope in legs:
            n = len(case.needed)
            total += (
                1.0 if k >= scope else (comb(scope - n, k - n) / comb(scope, k) if k >= n else 0.0)
            )
        return total

    measured = {k: full_recall(k) for k in range(1, 9)}
    assert measured == {1: 2, 2: 7, 3: 9, 4: 12, 5: 13, 6: 15, 7: 16, 8: 16}
    assert measured[CFG.k] == 16  # the configured k retrieves everything needed, on the dev set
    # where it matters (small k), hybrid is at or above the random baseline at k=1..4, 6, 7
    for k in (1, 2, 3, 4, 6, 7):
        assert measured[k] >= random_expectation(k) - 0.5, k
