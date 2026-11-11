"""Incorporation as first-class structure, and what it does (and must never do) to retrieval.

The date filter decides which version is IN EFFECT. An incorporated clause arrives only because an
in-effect clause names it, and its citation says so. A v2 purchase never retrieves a v1 clause
except through a recorded link."""

import dataclasses
from datetime import date
from typing import Any

import pytest
from sqlalchemy import Connection, text

from app.config import Settings
from app.domain.types import PurchaseDate
from app.retrieval.embedder import OnnxBgeEmbedder
from app.retrieval.hybrid import (
    HybridConfig,
    ScopeFilters,
    retrieval_scope_for,
    retrieve,
)
from app.retrieval.ingest import PolicyChangedError, ingest_policies
from app.retrieval.policy_loader import load_policies
from tests.db.helpers import expect_fail
from tests.fixtures.retrieval_cases import CASES

V1_DAY = PurchaseDate(date(2026, 2, 15))
V2_DAY = PurchaseDate(date(2026, 4, 1))
WIDE = HybridConfig(k=30, candidates_per_leg=60, rrf_k=60, weight_fts=1.0, weight_vec=1.0)
LINKS = {
    ("refund-v2-clause-5", "refund-v1-clause-5"),
    ("refund-v2-clause-5", "refund-v1-clause-6"),
    ("cancellation-v2-clause-1", "cancellation-v1-clause-1"),
    ("cancellation-v2-clause-1", "cancellation-v1-clause-2"),
    ("cancellation-v2-clause-1", "cancellation-v1-clause-3"),
}


@pytest.fixture(scope="module")
def embedder() -> OnnxBgeEmbedder:
    return OnnxBgeEmbedder()


@pytest.fixture
def corpus(conn: Connection, embedder: OnnxBgeEmbedder) -> Connection:
    ingest_policies(conn, embedder, load_policies())
    return conn


def _links(conn: Connection) -> set[tuple[str, str]]:
    rows = conn.execute(
        text("SELECT source_chunk_key, target_chunk_key FROM policy_incorporations")
    )
    return {(r.source_chunk_key, r.target_chunk_key) for r in rows}


# --- ingestion ---------------------------------------------------------------------------------


def test_links_are_ingested_idempotently_and_never_touch_chunk_identity(
    conn: Connection, embedder: OnnxBgeEmbedder
) -> None:
    docs = load_policies()
    first = ingest_policies(conn, embedder, docs)
    assert first.links_inserted == 5 and _links(conn) == LINKS
    ids_before = {
        r.chunk_key: r.id for r in conn.execute(text("SELECT id, chunk_key FROM policy_chunks"))
    }
    again = ingest_policies(conn, embedder, docs)
    assert (again.links_inserted, again.links_unchanged, again.chunks_inserted) == (0, 5, 0)
    ids_after = {
        r.chunk_key: r.id for r in conn.execute(text("SELECT id, chunk_key FROM policy_chunks"))
    }
    assert ids_before == ids_after


def test_a_link_declared_on_an_already_ingested_document_is_added_without_a_hash_error(
    conn: Connection, embedder: OnnxBgeEmbedder
) -> None:
    """The real migration path: v2 was ingested before it declared any link."""
    docs = load_policies()
    stripped = [dataclasses.replace(d, incorporations=()) for d in docs]
    ingest_policies(conn, embedder, stripped)
    assert _links(conn) == set()
    report = ingest_policies(conn, embedder, docs)  # same content hash: only the links are new
    assert report.documents_inserted == 0 and report.links_inserted == 5
    assert _links(conn) == LINKS


def test_a_stored_link_that_the_source_no_longer_declares_is_an_error(
    corpus: Connection, embedder: OnnxBgeEmbedder
) -> None:
    dropped = [
        dataclasses.replace(d, incorporations=()) if d.policy_type == "refund" else d
        for d in load_policies()
    ]
    with pytest.raises(PolicyChangedError, match="incorporation links not in source"):
        ingest_policies(corpus, embedder, dropped)


def test_audience_and_status_are_stored_and_cannot_silently_change(
    corpus: Connection, embedder: OnnxBgeEmbedder
) -> None:
    rows = {
        (r.policy_type, r.version): (r.audience, r.status)
        for r in corpus.execute(
            text("SELECT policy_type, version, audience, status FROM policy_documents")
        )
    }
    assert rows[("merchant_returns", 1)] == ("merchant", "current")
    assert rows[("faq_archive", 1)] == ("customer", "archived")
    assert rows[("refund", 2)] == ("customer", "current")
    flipped = [
        dataclasses.replace(d, status="archived")
        if d.policy_type == "refund" and d.version == 2
        else d
        for d in load_policies()
    ]
    with pytest.raises(PolicyChangedError, match="audience/status"):
        ingest_policies(corpus, embedder, flipped)
    state, _ = expect_fail(corpus, "UPDATE policy_documents SET status = 'archived'")
    assert state == "23001"  # immutable, like the rest of a published document


# --- the database's own rules for links --------------------------------------------------------


def _insert(src: str, dst: str) -> str:
    return (
        "INSERT INTO policy_incorporations (source_chunk_key, target_chunk_key)"  # noqa: S608
        f" VALUES ('{src}', '{dst}')"
    )


@pytest.mark.parametrize(
    ("src", "dst", "why"),
    [
        ("refund-v1-clause-1", "refund-v2-clause-1", "EARLIER"),  # backwards in time
        ("refund-v2-clause-1", "refund-v2-clause-2", "EARLIER"),  # same version
        ("refund-v2-clause-1", "warranty-v1-clause-1", "same policy"),  # across policies
        ("refund-v2-clause-5", "refund-v2-clause-5", ""),  # self
    ],
)
def test_the_database_rejects_links_that_are_not_same_policy_and_earlier(
    corpus: Connection, src: str, dst: str, why: str
) -> None:
    state, constraint = expect_fail(corpus, _insert(src, dst))
    assert state == "23514"  # check_violation (constraint or trigger)


def test_the_database_rejects_a_chain_so_one_hop_is_a_structural_guarantee(
    conn: Connection, embedder: OnnxBgeEmbedder
) -> None:
    """A synthetic v3 whose clause incorporates a v2 clause that itself incorporates v1."""
    docs = load_policies()
    refund = {d.version: d for d in docs if d.policy_type == "refund"}
    v2 = dataclasses.replace(refund[2], effective_to=date(2026, 12, 31))
    v3 = dataclasses.replace(
        refund[2],
        version=3,
        effective_from=date(2027, 1, 1),
        effective_to=None,
        source_path="policies/refund/v3.md",
        content_hash="3" * 64,
        incorporations=(),
    )
    rest = [d for d in docs if d.policy_type != "refund"]
    ingest_policies(conn, embedder, [*rest, refund[1], v2, v3])
    # v2-5 -> v1-5 exists; now try v3-5 -> v2-5, which would chain v3 -> v2 -> v1
    state, _ = expect_fail(conn, _insert("refund-v3-clause-5", "refund-v2-clause-5"))
    assert state == "23514"
    # and the reverse: v3-5 -> v1-5 is fine, but then v2-5 -> v1-5 targets a clause that is a target
    ok = conn.execute(text(_insert("refund-v3-clause-5", "refund-v1-clause-5")))
    assert ok.rowcount == 1


def test_links_are_append_only(corpus: Connection) -> None:
    for sql in (
        "UPDATE policy_incorporations SET target_chunk_key = 'refund-v1-clause-1'",
        "DELETE FROM policy_incorporations",
    ):
        assert expect_fail(corpus, sql)[0] == "23001"


# --- retrieval: direct versus incorporated -----------------------------------------------------


def test_retrieving_v2_clause_5_brings_v1_clause_5_with_it_flagged_and_with_provenance(
    corpus: Connection, embedder: OnnxBgeEmbedder
) -> None:
    res = retrieve(
        corpus,
        embedder,
        "I opened the digital add-on can I still get a refund",
        V2_DAY,
        ("refund",),
        WIDE,
    )
    direct = {r.clause.chunk_key: r for r in res.clauses}
    assert "refund-v2-clause-5" in direct
    assert not any(r.is_incorporated for r in res.clauses)  # direct means in effect
    assert {r.clause.chunk_key for r in res.incorporated} == {
        "refund-v1-clause-5",
        "refund-v1-clause-6",
    }
    assert all(
        r.incorporated_by is not None and r.incorporated_by.chunk_key == "refund-v2-clause-5"
        for r in res.incorporated
    )
    # the text the explainer needs is actually there
    texts = {r.clause.chunk_key: r.clause.text for r in res.incorporated}
    assert "non-refundable once accessed" in texts["refund-v1-clause-5"]
    # citations preserve provenance; never a bare v1 citation
    assert direct["refund-v2-clause-5"].citation == (
        "Refund Policy v2, clause 5, incorporating v1 clause 5, v1 clause 6"
    )
    cites = {r.clause.chunk_key: r.citation for r in res.incorporated}
    assert cites["refund-v1-clause-5"] == (
        "Refund Policy v1, clause 5, as incorporated by Refund Policy v2, clause 5"
    )
    assert all("incorporated by" in c for c in cites.values())


def test_incorporated_clauses_are_outside_the_purchase_date_validity_and_that_is_the_point(
    corpus: Connection, embedder: OnnxBgeEmbedder
) -> None:
    res = retrieve(corpus, embedder, "digital add-on", V2_DAY, ("refund",), WIDE)
    assert res.incorporated
    for r in res.incorporated:
        end = r.clause.valid_to_exclusive
        assert end is not None and end <= V2_DAY  # v1's validity ended before this purchase
    for r in res.clauses:
        assert r.clause.valid_from <= V2_DAY  # direct clauses are in effect


def _in_effect(c: object, day: date) -> bool:
    start = c.valid_from  # type: ignore[attr-defined]
    end = c.valid_to_exclusive  # type: ignore[attr-defined]
    return bool(start <= day and (end is None or day < end))


def test_a_clause_outside_the_purchase_date_is_retrieved_only_through_a_recorded_link(
    corpus: Connection, embedder: OnnxBgeEmbedder
) -> None:
    """For a v2 purchase, across every seeded case query and queries that QUOTE v1's wording, in
    several scopes: nothing whose validity misses the date is a DIRECT result, and every such
    clause that does appear is incorporated, through a link that is actually stored, by a clause
    that is itself in effect and itself in the results. (The shipping policy has only a v1, which
    IS in effect on a v2-era date, so a direct shipping v1 clause is correct and not a leak.)"""
    quoted = [
        "Refund window: 30 days from delivery date, or from the presumed-lost date.",
        "Auto-approve ceiling: 7500 cents. Above that, human review.",
        "Digital add-ons: non-refundable once accessed.",
        "Items flagged final_sale: non-refundable regardless of window.",
        "Annual plans: prorated refund of unused whole months, less a 10 percent fee, first 60 days.",
    ]
    queries = quoted + [q for c in CASES for q in c.queries]
    stored = _links(corpus)
    out_of_effect = 0
    for policy_types in (("refund",), ("cancellation",), ("refund", "cancellation", "shipping")):
        for q in queries:
            res = retrieve(corpus, embedder, q, V2_DAY, policy_types, WIDE)
            direct_keys = {r.clause.chunk_key for r in res.clauses}
            for r in res.clauses:
                assert _in_effect(r.clause, V2_DAY), (q, r.clause.chunk_key)  # never out of range
                if r.clause.policy_type in ("refund", "cancellation"):
                    assert r.clause.version == 2, (q, r.clause.chunk_key)  # v2 is in effect
            for r in res.incorporated:
                out_of_effect += 1
                assert not _in_effect(r.clause, V2_DAY)  # arrived by link, not by validity
                assert r.incorporated_by is not None
                assert (r.incorporated_by.chunk_key, r.clause.chunk_key) in stored  # a real link
                assert r.incorporated_by.chunk_key in direct_keys  # its source is in the results
                assert _in_effect(r.incorporated_by, V2_DAY)  # and the source is in effect
                assert "incorporated by" in r.citation
    assert out_of_effect > 0  # the property was exercised, not vacuous


def test_a_v1_purchase_never_retrieves_any_v2_clause_incorporation_only_points_backwards(
    corpus: Connection, embedder: OnnxBgeEmbedder
) -> None:
    for q in ("digital add-on refund", "as v1 but fee removed cancel annual", "cooling-off period"):
        res = retrieve(corpus, embedder, q, V1_DAY, ("refund", "cancellation"), WIDE)
        assert {r.clause.version for r in res.context()} == {1}
        assert res.incorporated == ()


def test_an_incorporated_clause_never_appears_without_its_in_effect_source(
    corpus: Connection, embedder: OnnxBgeEmbedder
) -> None:
    tiny = HybridConfig(k=1, candidates_per_leg=60, rrf_k=60, weight_fts=1.0, weight_vec=1.0)
    res = retrieve(
        corpus, embedder, "cooling-off period refund first charge", V2_DAY, ("cancellation",), tiny
    )
    assert [r.clause.chunk_key for r in res.clauses] == ["cancellation-v2-clause-2"]
    assert res.incorporated == ()  # clause 2 incorporates nothing; clause 1 was not retrieved


def test_following_can_be_switched_off_and_is_one_hop_only(
    corpus: Connection, embedder: OnnxBgeEmbedder
) -> None:
    off = retrieve(corpus, embedder, "digital add-on", V2_DAY, ("refund",), WIDE, follow=False)
    assert off.incorporated == () and not any(r.incorporates for r in off.clauses)
    on = retrieve(corpus, embedder, "digital add-on", V2_DAY, ("refund",), WIDE)
    # nothing incorporated is itself followed: no incorporated clause carries links of its own
    assert all(r.incorporates == () for r in on.incorporated)


def test_incorporated_clauses_are_additional_to_k(
    corpus: Connection, embedder: OnnxBgeEmbedder
) -> None:
    res = retrieve(
        corpus,
        embedder,
        "digital add-on opened refund",
        V2_DAY,
        ("refund",),
        HybridConfig(2, 60, 60, 1.0, 1.0),
    )
    assert len(res.clauses) <= 2
    assert len(res.context()) == len(res.clauses) + len(res.incorporated)


# --- the expanded corpus: what the metadata filters are worth -----------------------------------


def test_merchant_and_archived_documents_are_excluded_by_default_but_retrievable_without_filters(
    corpus: Connection, embedder: OnnxBgeEmbedder
) -> None:
    seller = "must the seller accept a return within 30 days of delivery and refund the buyer"
    scope = ("refund", "merchant_returns")
    default = retrieve(corpus, embedder, seller, V2_DAY, scope, WIDE)
    assert not {r.clause.policy_type for r in default.clauses} & {"merchant_returns"}
    open_ = retrieve(corpus, embedder, seller, V2_DAY, scope, WIDE, ScopeFilters(False, False))
    assert "merchant_returns" in {r.clause.policy_type for r in open_.clauses}  # the trap exists

    faq = "how long do I have to ask for a refund after my order arrives"
    scope = ("refund", "faq_archive")
    default = retrieve(corpus, embedder, faq, V2_DAY, scope, WIDE)
    assert "faq_archive" not in {r.clause.policy_type for r in default.clauses}
    open_ = retrieve(corpus, embedder, faq, V2_DAY, scope, WIDE, ScopeFilters(False, False))
    # The archived FAQ is open-ended, so the DATE filter alone does not remove it from a v2
    # purchase: only the status filter does. This is the naive-retriever trap, demonstrated.
    assert "faq_archive" in {r.clause.policy_type for r in open_.clauses}


def test_the_broad_scope_includes_the_plausible_distractor_types_but_not_merchant_or_archived() -> (
    None
):
    scope = retrieval_scope_for(["refund_request"])
    assert set(scope) == {"refund", "gift_cards", "warranty"}
    assert "merchant_returns" not in scope and "faq_archive" not in scope
    assert "shipping_restrictions" in retrieval_scope_for(["refund_request"], delivery_issue=True)


def test_every_new_document_has_a_valid_date_and_exactly_one_version_in_effect_per_day(
    corpus: Connection,
) -> None:
    bad = corpus.execute(
        text(
            "SELECT d::date AS day, t.policy_type, count(DISTINCT c.version) AS n"
            " FROM generate_series(DATE '2025-06-01', DATE '2027-01-01', INTERVAL '1 day') AS d"
            " CROSS JOIN (VALUES ('gift_cards'), ('warranty'), ('shipping_restrictions'),"
            "   ('merchant_returns'), ('faq_archive')) AS t(policy_type)"
            " LEFT JOIN policy_chunks c ON c.policy_type = t.policy_type AND c.validity @> d::date"
            " GROUP BY d, t.policy_type HAVING count(DISTINCT c.version) <> 1"
        )
    ).all()
    assert bad == []


def test_the_settings_default_k_is_configuration() -> None:
    settings: Any = Settings
    s = settings(_env_file=None)
    assert HybridConfig.from_settings(s).k == s.retrieval_k
