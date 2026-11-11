"""Policy ingestion and date-filtered retrieval, against real PostgreSQL."""

from datetime import date

import pytest
from sqlalchemy import Connection, text

from app.domain.types import PurchaseDate
from app.retrieval.embedder import OnnxBgeEmbedder
from app.retrieval.embedding_model import EMBEDDING_MODEL_ID, EMBEDDING_MODEL_REVISION
from app.retrieval.ingest import (
    EmbeddingModelMismatchError,
    PolicyChangedError,
    ingest_policies,
)
from app.retrieval.policy_loader import load_policies
from app.retrieval.policy_store import clauses_in_effect, search_clauses_fts
from tests.db.helpers import expect_fail


@pytest.fixture(scope="module")
def embedder() -> OnnxBgeEmbedder:
    return OnnxBgeEmbedder()


@pytest.fixture
def ingested(conn: Connection, embedder: OnnxBgeEmbedder) -> Connection:
    ingest_policies(conn, embedder, load_policies())
    return conn


def _pd(y: int, m: int, d: int) -> PurchaseDate:
    return PurchaseDate(date(y, m, d))


def _versions(clauses: list) -> set[int]:  # type: ignore[type-arg]
    return {c.version for c in clauses}


# --- idempotent re-ingestion --------------------------------------------------------------


def _snapshot(conn: Connection) -> list[tuple[str, int, str]]:
    rows = conn.execute(
        text("SELECT chunk_key, clause_number, text FROM policy_chunks ORDER BY chunk_key")
    )
    return [(r.chunk_key, r.clause_number, r.text) for r in rows]


def test_reingestion_twice_leaves_chunk_count_and_identifiers_unchanged(
    conn: Connection, embedder: OnnxBgeEmbedder
) -> None:
    docs = load_policies()
    first = ingest_policies(conn, embedder, docs)
    assert first.documents_inserted == 5 and first.chunks_inserted == 19
    snap1 = _snapshot(conn)
    ids1 = {
        r.chunk_key: r.id for r in conn.execute(text("SELECT id, chunk_key FROM policy_chunks"))
    }

    second = ingest_policies(conn, embedder, docs)
    third = ingest_policies(conn, embedder, docs)
    assert second.documents_inserted == third.documents_inserted == 0
    assert second.chunks_inserted == third.chunks_inserted == 0
    assert second.chunks_unchanged == 19

    assert _snapshot(conn) == snap1
    assert len(snap1) == 19 and len({k for k, _, _ in snap1}) == 19
    ids2 = {
        r.chunk_key: r.id for r in conn.execute(text("SELECT id, chunk_key FROM policy_chunks"))
    }
    assert ids1 == ids2  # not even the row ids change: nothing was re-inserted


def test_chunk_identifier_scheme_is_human_meaningful(ingested: Connection) -> None:
    keys = {r.chunk_key for r in ingested.execute(text("SELECT chunk_key FROM policy_chunks"))}
    assert "refund-v2-clause-3" in keys
    assert "shipping-v1-clause-1" in keys
    assert "cancellation-v1-clause-3" in keys


def test_chunks_carry_denormalized_type_version_validity_and_model_identity(
    ingested: Connection,
) -> None:
    rows = ingested.execute(
        text(
            "SELECT chunk_key, policy_type, version, lower(validity) AS lo, upper(validity) AS hi,"
            " embedding_model, embedding_model_revision, injection_verdict,"
            " vector_dims(embedding) AS dims FROM policy_chunks"
        )
    ).all()
    assert len(rows) == 19
    for r in rows:
        assert (r.embedding_model, r.embedding_model_revision) == (
            EMBEDDING_MODEL_ID,
            EMBEDDING_MODEL_REVISION,
        )
        assert r.dims == 384
        assert r.injection_verdict == "unscanned"  # filled when Prompt Guard lands
    r2 = next(r for r in rows if r.chunk_key == "refund-v2-clause-1")
    assert (r2.lo, r2.hi, r2.version) == (date(2026, 3, 1), None, 2)
    r1 = next(r for r in rows if r.chunk_key == "refund-v1-clause-1")
    assert (r1.lo, r1.hi) == (date(2025, 6, 1), date(2026, 3, 1))


def test_changed_policy_text_without_new_version_fails(
    conn: Connection, embedder: OnnxBgeEmbedder
) -> None:
    docs = load_policies()
    ingest_policies(conn, embedder, docs)
    import dataclasses

    edited = [
        dataclasses.replace(d, content_hash="0" * 64) if d.policy_type == "refund" else d
        for d in docs
    ]
    with pytest.raises(PolicyChangedError):
        ingest_policies(conn, embedder, edited)


def test_vectors_from_a_different_embedding_model_are_detected(
    ingested: Connection,
) -> None:
    class OtherModel:
        model_id = "some/other-model"
        revision = "deadbeef"
        dim = 384

        def embed(self, texts):  # type: ignore[no-untyped-def]
            raise AssertionError("must fail before embedding anything")

    with pytest.raises(EmbeddingModelMismatchError):
        ingest_policies(ingested, OtherModel(), load_policies())


# --- date-filtered retrieval ------------------------------------------------------------------


def test_purchase_before_cutover_returns_v1_never_v2(ingested: Connection) -> None:
    for d in (_pd(2025, 6, 1), _pd(2026, 2, 20), _pd(2026, 2, 28)):
        clauses = clauses_in_effect(ingested, "refund", d)
        assert _versions(clauses) == {1}, d
        assert len(clauses) == 6
        found = search_clauses_fts(ingested, "refund window days", d, ("refund",))
        assert found and _versions(found) == {1}, d


def test_purchase_after_cutover_returns_v2_never_v1(ingested: Connection) -> None:
    for d in (_pd(2026, 3, 2), _pd(2026, 11, 10), _pd(2030, 1, 1)):
        clauses = clauses_in_effect(ingested, "refund", d)
        assert _versions(clauses) == {2}, d
        assert len(clauses) == 5
        found = search_clauses_fts(ingested, "refund window days", d, ("refund",))
        assert found and _versions(found) == {2}, d


def test_date_exactly_on_cutover_returns_exactly_one_version(ingested: Connection) -> None:
    on = clauses_in_effect(ingested, "refund", _pd(2026, 3, 1))
    assert _versions(on) == {2}
    day_before = clauses_in_effect(ingested, "refund", _pd(2026, 2, 28))
    assert _versions(day_before) == {1}
    for policy_type in ("refund", "cancellation"):
        assert len(_versions(clauses_in_effect(ingested, policy_type, _pd(2026, 3, 1)))) == 1


def test_every_day_in_range_has_exactly_one_version_per_policy_type(ingested: Connection) -> None:
    bad = ingested.execute(
        text(
            "SELECT d::date AS day, t.policy_type, count(DISTINCT c.version) AS n"
            " FROM generate_series(DATE '2025-06-01', DATE '2027-01-01', INTERVAL '1 day') AS d"
            " CROSS JOIN (VALUES ('shipping'), ('refund'), ('cancellation')) AS t(policy_type)"
            " LEFT JOIN policy_chunks c ON c.policy_type = t.policy_type AND c.validity @> d::date"
            " GROUP BY d, t.policy_type HAVING count(DISTINCT c.version) <> 1"
        )
    ).all()
    assert bad == []


def test_date_before_any_policy_exists_returns_nothing(ingested: Connection) -> None:
    assert clauses_in_effect(ingested, "refund", _pd(2025, 5, 31)) == []
    assert search_clauses_fts(ingested, "refund", _pd(2025, 5, 31)) == []


def test_citation_label_is_human_checkable(ingested: Connection) -> None:
    clauses = clauses_in_effect(ingested, "refund", _pd(2026, 3, 1))
    third = next(c for c in clauses if c.clause_number == 3)
    assert third.chunk_key == "refund-v2-clause-3"
    assert third.citation == "Refund Policy v2, clause 3"
    assert "delivery exception" in third.text


# --- database-level protections ------------------------------------------------------------------


def test_chunk_key_scheme_and_denormalized_columns_are_enforced_by_the_database(
    ingested: Connection,
) -> None:
    state, constraint = expect_fail(
        ingested,
        "UPDATE policy_chunks SET chunk_key = 'whatever' WHERE chunk_key = 'refund-v2-clause-1'",
    )
    assert state == "23001"  # immutable identity (trigger) before the CHECK is reached
    # A new chunk whose copied version disagrees with its document is rejected by the FK.
    state, constraint = expect_fail(
        ingested,
        "INSERT INTO policy_chunks (document_id, ordinal, text, embedding, embedding_model,"
        " embedding_model_revision, chunk_key, clause_number, policy_type, version, validity)"
        " SELECT d.id, 99, 'x', (SELECT embedding FROM policy_chunks LIMIT 1), 'm', 'r',"
        " 'refund-v1-clause-99', 99, 'refund', 1, d.validity FROM policy_documents d"
        " WHERE d.policy_type = 'refund' AND d.version = 2",
    )
    assert (state, constraint) == ("23503", "policy_chunks_document_identity_fk")


def test_chunk_key_must_follow_the_scheme(ingested: Connection) -> None:
    state, constraint = expect_fail(
        ingested,
        "INSERT INTO policy_chunks (document_id, ordinal, text, embedding, embedding_model,"
        " embedding_model_revision, chunk_key, clause_number, policy_type, version, validity)"
        " SELECT d.id, 99, 'x', (SELECT embedding FROM policy_chunks LIMIT 1), 'm', 'r',"
        " 'chunk-7', 99, d.policy_type, d.version, d.validity FROM policy_documents d"
        " WHERE d.policy_type = 'refund' AND d.version = 2",
    )
    assert (state, constraint) == ("23514", "policy_chunks_key_scheme")


def test_published_policy_text_and_documents_are_immutable(ingested: Connection) -> None:
    state, _ = expect_fail(ingested, "UPDATE policy_chunks SET text = 'edited'")
    assert state == "23001"
    state, _ = expect_fail(ingested, "UPDATE policy_documents SET content_hash = 'x'")
    assert state == "23001"
