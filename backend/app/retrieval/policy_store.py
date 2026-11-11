"""Date-filtered policy lookup. The date filter lives IN SQL and uses the denormalized
`policy_type` and `validity` columns on policy_chunks (no join), so a wrong-version clause
cannot be returned by any ranking step layered on top.

The date passed here is ALWAYS the purchase date (it selects the policy version). The refund
window anchor (delivery / presumed-lost date) is a different date and never goes here; the
distinct types make mixing them a mypy error.
"""

from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import Connection, Row, text

from app.domain.types import PurchaseDate

_LABELS = {"faq_archive": "Archived FAQ"}


@dataclass(frozen=True)
class PolicyClause:
    chunk_key: str
    policy_type: str
    version: int
    clause_number: int
    text: str
    valid_from: date
    valid_to_exclusive: date | None

    @property
    def citation(self) -> str:
        """Human-checkable label, e.g. 'Refund Policy v2, clause 3'."""
        label = _LABELS.get(self.policy_type) or (
            " ".join(w.capitalize() for w in self.policy_type.split("_")) + " Policy"
        )
        return f"{label} v{self.version}, clause {self.clause_number}"


_COLUMNS = (
    "chunk_key, policy_type, version, clause_number, text,"
    " lower(validity) AS valid_from, upper(validity) AS valid_to"
)


def _to_clause(r: Row[Any]) -> PolicyClause:
    return PolicyClause(
        r.chunk_key,
        r.policy_type,
        r.version,
        r.clause_number,
        r.text,
        r.valid_from,
        r.valid_to,
    )


def clauses_in_effect(
    conn: Connection, policy_type: str, purchase_date: PurchaseDate
) -> list[PolicyClause]:
    """Every clause of the policy version in effect on the purchase date (empty if none)."""
    rows = conn.execute(
        text(
            f"SELECT {_COLUMNS} FROM policy_chunks"  # noqa: S608 (constant column list)
            " WHERE policy_type = :t AND validity @> CAST(:d AS date)"
            " ORDER BY version, clause_number"
        ),
        {"t": policy_type, "d": purchase_date},
    )
    return [_to_clause(r) for r in rows]


def search_clauses_fts(
    conn: Connection,
    query: str,
    purchase_date: PurchaseDate,
    policy_types: tuple[str, ...] | None = None,
    limit: int = 5,
) -> list[PolicyClause]:
    """Full-text search restricted to the versions in effect on the purchase date.
    (Vector search and rank fusion are added in the retrieval-baseline task and must apply
    this same filter.)"""
    rows = conn.execute(
        text(
            f"SELECT {_COLUMNS} FROM policy_chunks"  # noqa: S608 (constant column list)
            " WHERE validity @> CAST(:d AS date)"
            "   AND (CAST(:types AS text[]) IS NULL OR policy_type = ANY(CAST(:types AS text[])))"
            "   AND tsv @@ websearch_to_tsquery('english', :q)"
            " ORDER BY ts_rank(tsv, websearch_to_tsquery('english', :q)) DESC, chunk_key"
            " LIMIT :limit"
        ),
        {
            "d": purchase_date,
            "types": list(policy_types) if policy_types else None,
            "q": query,
            "limit": limit,
        },
    )
    return [_to_clause(r) for r in rows]
