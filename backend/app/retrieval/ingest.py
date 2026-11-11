"""Idempotent policy ingestion: documents and clause-level chunks with embeddings.

    uv run python -m app.retrieval.ingest [policies_dir]

Running it twice produces the same chunk set with the same identifiers and no duplicates.
Policies are versioned and immutable: if a stored document's text differs from the file
(content hash) the run FAILS and asks for a new version rather than silently rewriting what
past citations pointed at. Stored vectors made by a different embedding model or revision
also fail loudly, so a model change is detectable instead of silent.
"""

import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Connection, create_engine, text

from app.config import get_settings
from app.retrieval.embedder import Embedder, OnnxBgeEmbedder
from app.retrieval.policy_loader import DEFAULT_POLICIES_DIR, PolicyDocument, load_policies


class PolicyChangedError(RuntimeError):
    """A stored policy differs from its source file without a version bump."""


class EmbeddingModelMismatchError(RuntimeError):
    """Stored vectors were produced by a different embedding model or revision."""


@dataclass(frozen=True)
class IngestReport:
    documents_inserted: int
    chunks_inserted: int
    chunks_unchanged: int


def vector_literal(vec: Sequence[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def ingest_policies(
    conn: Connection, embedder: Embedder, docs: Sequence[PolicyDocument]
) -> IngestReport:
    """Insert missing documents/chunks; verify existing ones. Call inside a transaction."""
    # Serialize concurrent ingestion runs for the duration of this transaction.
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('policy_ingest'))"))
    docs_inserted = chunks_inserted = chunks_unchanged = 0

    for doc in docs:
        row = conn.execute(
            text(
                "SELECT id, content_hash, lower(validity) AS lo, upper(validity) AS hi"
                " FROM policy_documents WHERE policy_type = :t AND version = :v"
            ),
            {"t": doc.policy_type, "v": doc.version},
        ).one_or_none()
        if row is None:
            doc_id = conn.execute(
                text(
                    "INSERT INTO policy_documents (policy_type, version, validity, title,"
                    " source_path, content_hash)"
                    " VALUES (:t, :v, daterange(:lo, :hi, '[)'), :title, :src, :h) RETURNING id"
                ),
                {
                    "t": doc.policy_type,
                    "v": doc.version,
                    "lo": doc.valid_from,
                    "hi": doc.valid_to_exclusive,
                    "title": doc.title,
                    "src": doc.source_path,
                    "h": doc.content_hash,
                },
            ).scalar_one()
            docs_inserted += 1
        else:
            doc_id = row.id
            if row.content_hash != doc.content_hash:
                raise PolicyChangedError(
                    f"{doc.source_path}: text changed but {doc.policy_type} v{doc.version} is "
                    "already ingested. Publish a new version instead of editing an old one."
                )

        existing = {
            r.chunk_key: r
            for r in conn.execute(
                text(
                    "SELECT chunk_key, text, embedding_model, embedding_model_revision"
                    " FROM policy_chunks WHERE document_id = :d"
                ),
                {"d": doc_id},
            )
        }
        expected_keys = {doc.chunk_key(c.number) for c in doc.clauses}
        stray = set(existing) - expected_keys
        if stray:
            raise PolicyChangedError(f"{doc.source_path}: stored chunks not in source: {stray}")

        to_embed = []
        for clause in doc.clauses:
            key = doc.chunk_key(clause.number)
            have = existing.get(key)
            if have is None:
                to_embed.append(clause)
                continue
            if have.text != clause.text:
                raise PolicyChangedError(f"{key}: stored clause text differs from source")
            if (have.embedding_model, have.embedding_model_revision) != (
                embedder.model_id,
                embedder.revision,
            ):
                raise EmbeddingModelMismatchError(
                    f"{key}: stored vector is from {have.embedding_model}@"
                    f"{have.embedding_model_revision}, current embedder is "
                    f"{embedder.model_id}@{embedder.revision}; re-embed deliberately"
                )
            chunks_unchanged += 1

        if to_embed:
            vectors = embedder.embed([doc.embedding_text(c) for c in to_embed])
            for ordinal_clause, vec in zip(to_embed, vectors, strict=True):
                conn.execute(
                    text(
                        "INSERT INTO policy_chunks (document_id, ordinal, text, embedding,"
                        " embedding_model, embedding_model_revision, chunk_key, clause_number,"
                        " policy_type, version, validity)"
                        " VALUES (:d, :ord, :txt, CAST(:emb AS vector), :model, :rev, :key, :n,"
                        " :t, :v, daterange(:lo, :hi, '[)'))"
                    ),
                    {
                        "d": doc_id,
                        "ord": ordinal_clause.number - 1,
                        "txt": ordinal_clause.text,
                        "emb": vector_literal(vec),
                        "model": embedder.model_id,
                        "rev": embedder.revision,
                        "key": doc.chunk_key(ordinal_clause.number),
                        "n": ordinal_clause.number,
                        "t": doc.policy_type,
                        "v": doc.version,
                        "lo": doc.valid_from,
                        "hi": doc.valid_to_exclusive,
                    },
                )
            chunks_inserted += len(to_embed)

    return IngestReport(docs_inserted, chunks_inserted, chunks_unchanged)


def main(argv: list[str]) -> None:
    policies_dir = Path(argv[0]) if argv else DEFAULT_POLICIES_DIR
    docs = load_policies(policies_dir)
    engine = create_engine(get_settings().database_url)
    with engine.begin() as conn:
        report = ingest_policies(conn, OnnxBgeEmbedder(), docs)
    print(report)


if __name__ == "__main__":
    main(sys.argv[1:])
