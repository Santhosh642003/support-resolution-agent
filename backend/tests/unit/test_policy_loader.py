"""Policy corpus: exact PM-specified content, stable clause identifiers, no gaps in time.
No database or network needed."""

from datetime import date
from pathlib import Path

import pytest

from app.retrieval.policy_loader import (
    PolicyFormatError,
    load_policies,
    parse_policy,
    validate_corpus,
)

# The ORIGINAL three policies. The corpus was later expanded with distractor documents
# (task 7C); these assertions are about the original documents and are unchanged in substance.
ORIGINAL_TYPES = ("shipping", "refund", "cancellation")
ALL_DOCS = load_policies()
DOCS = {(d.policy_type, d.version): d for d in ALL_DOCS if d.policy_type in ORIGINAL_TYPES}


def _clause(policy_type: str, version: int, number: int) -> str:
    return next(c.text for c in DOCS[(policy_type, version)].clauses if c.number == number)


def test_expected_documents_and_clause_counts() -> None:
    counts = {k: len(d.clauses) for k, d in DOCS.items()}
    assert counts == {
        ("shipping", 1): 4,  # clause 4 added 2026-11-12 (SPEC 18.6)
        ("refund", 1): 6,
        ("refund", 2): 6,  # residual clause 6 added 2026-11-12
        ("cancellation", 1): 3,
        ("cancellation", 2): 2,
    }


def test_chunk_keys_are_human_meaningful_and_stable() -> None:
    assert DOCS[("refund", 2)].chunk_key(3) == "refund-v2-clause-3"
    keys = [d.chunk_key(c.number) for d in DOCS.values() for c in d.clauses]
    assert len(keys) == len(set(keys)) == 21
    assert "shipping-v1-clause-2" in keys and "cancellation-v2-clause-2" in keys


def test_pm_specified_numbers_and_rules_are_present_exactly() -> None:
    assert "30 days from delivery date" in _clause("refund", 1, 1)
    assert "7500 cents" in _clause("refund", 1, 3)
    assert "45 days" in _clause("refund", 2, 1)
    assert "12000 cents" in _clause("refund", 2, 2)
    assert "delivery exception" in _clause("refund", 2, 3)
    assert "never list price" in _clause("refund", 2, 4)
    assert "7 business days" in _clause("shipping", 1, 1)
    assert "10 business days" in _clause("shipping", 1, 2)
    assert "10 percent administrative fee" in _clause("cancellation", 1, 3)
    assert "first 60 days" in _clause("cancellation", 1, 3)
    assert "14-day cooling-off" in _clause("cancellation", 2, 2)


def test_validity_ranges_match_the_policy_text() -> None:
    r1, r2 = DOCS[("refund", 1)], DOCS[("refund", 2)]
    assert (r1.effective_from, r1.effective_to) == (date(2025, 6, 1), date(2026, 2, 28))
    assert r1.valid_to_exclusive == date(2026, 3, 1) == r2.valid_from
    assert r2.effective_to is None and r2.valid_to_exclusive is None
    assert DOCS[("shipping", 1)].valid_to_exclusive is None  # single version, open-ended


def test_every_policy_is_labeled_simulated() -> None:
    for path in (Path(__file__).resolve().parents[3] / "policies").glob("*/v*.md"):
        text = path.read_text(encoding="utf-8")
        assert "simulated: true" in text and "SIMULATED BUSINESS" in text, path


def test_content_hash_ignores_line_endings() -> None:
    src = Path(__file__).resolve().parents[3] / "policies" / "refund" / "v2.md"
    text = src.read_text(encoding="utf-8").replace("\r\n", "\n")
    a = parse_policy(text, "policies/refund/v2.md")
    b = parse_policy(text.replace("\n", "\r\n"), "policies/refund/v2.md")
    assert a.content_hash == b.content_hash == DOCS[("refund", 2)].content_hash


def test_embedding_text_carries_the_citation_label() -> None:
    doc = DOCS[("refund", 2)]
    assert doc.embedding_text(doc.clauses[2]).startswith("Refund Policy v2, clause 3: ")


_GOOD = """---
policy_type: refund
version: 1
effective_from: 2025-06-01
title: Refund Policy
simulated: true
---
1. One.
2. Two.
"""


def test_parser_accepts_good_and_rejects_bad_clause_numbering() -> None:
    assert len(parse_policy(_GOOD, "x").clauses) == 2
    with pytest.raises(PolicyFormatError, match="explicit, unique"):
        parse_policy(_GOOD.replace("2. Two.", "3. Three."), "x")
    with pytest.raises(PolicyFormatError, match="explicit, unique"):
        parse_policy(_GOOD.replace("2. Two.", "1. Again."), "x")


def test_parser_requires_simulated_label_and_valid_front_matter() -> None:
    with pytest.raises(PolicyFormatError, match="simulated"):
        parse_policy(_GOOD.replace("simulated: true", "simulated: false"), "x")
    with pytest.raises(PolicyFormatError, match="missing front matter"):
        parse_policy(_GOOD.replace("title: Refund Policy\n", ""), "x")
    with pytest.raises(PolicyFormatError, match="unknown policy_type"):
        parse_policy(_GOOD.replace("refund", "lottery", 1), "x")  # "warranty" is a real type now
    with pytest.raises(PolicyFormatError, match="before effective_from"):
        parse_policy(_GOOD.replace("title:", "effective_to: 2025-01-01\ntitle:"), "x")


def test_corpus_validation_rejects_gaps_overlaps_and_open_ended_middle_versions() -> None:
    v1 = parse_policy(_GOOD.replace("title:", "effective_to: 2026-02-28\ntitle:"), "v1")
    v2 = parse_policy(
        _GOOD.replace("version: 1", "version: 2").replace("2025-06-01", "2026-03-01"), "v2"
    )
    validate_corpus([v1, v2])  # adjacent: fine
    gap = parse_policy(
        _GOOD.replace("version: 1", "version: 2").replace("2025-06-01", "2026-03-05"), "g"
    )
    with pytest.raises(PolicyFormatError, match="gap or overlap"):
        validate_corpus([v1, gap])
    overlap = parse_policy(
        _GOOD.replace("version: 1", "version: 2").replace("2025-06-01", "2026-02-01"), "o"
    )
    with pytest.raises(PolicyFormatError, match="gap or overlap"):
        validate_corpus([v1, overlap])
    open_v1 = parse_policy(_GOOD, "open")
    with pytest.raises(PolicyFormatError, match="open-ended"):
        validate_corpus([open_v1, v2])
