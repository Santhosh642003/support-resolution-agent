"""One capability: everything the order-status workflow needs from the policy corpus.

Counted as a SINGLE tool call (SPEC 10.5): the shipping policy version in effect on the purchase
date, the scoped retrieval that supplies context text, and the text of that version's clauses (from
which the clauses the rules APPLIED are looked up by key). Retrieval still cannot add, remove or
change a citation; this only bundles the reads so the count measures capabilities, not statements.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Connection

from app.domain.types import PurchaseDate
from app.retrieval.embedder import Embedder
from app.retrieval.hybrid import HybridConfig, RetrievalResult, ScopeRecord, retrieve
from app.retrieval.policy_store import PolicyClause, clauses_in_effect, policy_version_in_effect


@dataclass(frozen=True)
class PolicyContext:
    version: int  # 0 when no shipping policy was in effect on the purchase date
    document_id: UUID | None
    retrieved: RetrievalResult
    clauses: dict[str, PolicyClause]  # the shipping clauses of that version, by chunk key


def policy_context(
    conn: Connection,
    embedder: Embedder,
    message: str,
    purchase: PurchaseDate,
    scope: ScopeRecord,
    hybrid: HybridConfig,
) -> PolicyContext:
    found = policy_version_in_effect(conn, "shipping", purchase)
    retrieved = retrieve(conn, embedder, message, purchase, scope.types, hybrid, scope=scope)
    by_key = {c.chunk_key: c for c in clauses_in_effect(conn, "shipping", purchase)}
    return PolicyContext(
        0 if found is None else found[0], None if found is None else found[1], retrieved, by_key
    )
