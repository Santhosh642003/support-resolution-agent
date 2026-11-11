"""Incorporation links in policy front matter: parsing, validation and the content hash."""

import hashlib

import pytest

from app.retrieval.policy_loader import (
    Incorporation,
    PolicyFormatError,
    load_policies,
    parse_incorporations,
    parse_policy,
    validate_corpus,
)

V1 = """---
policy_type: refund
version: 1
effective_from: 2025-06-01
effective_to: 2026-02-28
title: Refund Policy
simulated: true
---
SIMULATED BUSINESS.

1. One.
2. Two.
"""

V2 = """---
policy_type: refund
version: 2
effective_from: 2026-03-01
title: Refund Policy
simulated: true
incorporates: 2 <- refund-v1-clause-1, refund-v1-clause-2
---
SIMULATED BUSINESS.

1. One.
2. Unchanged.
"""


def corpus(v2: str = V2, v1: str = V1) -> list:  # type: ignore[type-arg]
    return [parse_policy(v1, "refund/v1.md"), parse_policy(v2, "refund/v2.md")]


def test_the_front_matter_declaration_is_parsed_into_clause_to_targets() -> None:
    assert parse_incorporations("5 <- a-v1-clause-5, a-v1-clause-6; 2 <- a-v1-clause-3", "x") == (
        Incorporation(5, ("a-v1-clause-5", "a-v1-clause-6")),
        Incorporation(2, ("a-v1-clause-3",)),
    )
    assert parse_incorporations("", "x") == ()


@pytest.mark.parametrize(
    "bad",
    [
        "5 refund-v1-clause-5",  # no arrow
        "five <- refund-v1-clause-5",  # clause is not a number
        "5 <- ",  # no targets
        "5 <- refund-v1-clause-5, refund-v1-clause-5",  # duplicate target
        "5 <- refund-v1-clause-5; 5 <- refund-v1-clause-6",  # clause listed twice
    ],
)
def test_malformed_declarations_are_rejected(bad: str) -> None:
    with pytest.raises(PolicyFormatError):
        parse_incorporations(bad, "x")


def test_a_valid_link_validates() -> None:
    validate_corpus(corpus())


def test_a_link_to_an_unknown_clause_is_rejected() -> None:
    bad = V2.replace("refund-v1-clause-2", "refund-v1-clause-9")
    with pytest.raises(PolicyFormatError, match="unknown clause"):
        validate_corpus(corpus(bad))


def test_a_link_must_name_an_earlier_version_of_the_same_policy() -> None:
    later = V2.replace(
        "incorporates: 2 <- refund-v1-clause-1, refund-v1-clause-2",
        "incorporates: 2 <- refund-v2-clause-1",
    )
    with pytest.raises(PolicyFormatError, match="EARLIER version"):
        validate_corpus(corpus(later))

    no_link = V2.replace("incorporates: 2 <- refund-v1-clause-1, refund-v1-clause-2\n", "")
    other = parse_policy(
        no_link.replace("refund", "warranty")
        .replace("version: 2", "version: 1")
        .replace("effective_from: 2026-03-01", "effective_from: 2025-06-01"),
        "warranty/v1.md",
    )
    cross = V2.replace("refund-v1-clause-2", "warranty-v1-clause-1")
    with pytest.raises(PolicyFormatError, match="same policy"):
        validate_corpus([other, *corpus(cross)])


def test_the_source_clause_must_exist_in_its_own_document() -> None:
    bad = V2.replace("incorporates: 2 <-", "incorporates: 7 <-")
    with pytest.raises(PolicyFormatError, match="does not exist"):
        validate_corpus(corpus(bad))


def test_a_chain_of_two_hops_is_a_finding_not_a_feature() -> None:
    """v2 clause 2 incorporates v1; a v3 clause then incorporates that v2 clause: a chain."""
    v2_closed = V2.replace("title: Refund Policy", "effective_to: 2026-12-31\ntitle: Refund Policy")
    v3 = (
        V2.replace("version: 2", "version: 3")
        .replace("effective_from: 2026-03-01", "effective_from: 2027-01-01")
        .replace(
            "incorporates: 2 <- refund-v1-clause-1, refund-v1-clause-2",
            "incorporates: 2 <- refund-v2-clause-2",
        )
    )
    docs = [parse_policy(V1, "a"), parse_policy(v2_closed, "b"), parse_policy(v3, "c")]
    with pytest.raises(PolicyFormatError, match="chains are not supported"):
        validate_corpus(docs)


# --- the content hash: metadata never changes what a citation depends on --------------------------


def test_incorporation_audience_and_status_lines_do_not_change_the_content_hash() -> None:
    plain = V2.replace("incorporates: 2 <- refund-v1-clause-1, refund-v1-clause-2\n", "")
    assert parse_policy(V2, "x").content_hash == parse_policy(plain, "x").content_hash
    with_meta = plain.replace(
        "simulated: true", "simulated: true\naudience: customer\nstatus: current"
    )
    assert parse_policy(with_meta, "x").content_hash == parse_policy(plain, "x").content_hash


def test_the_hash_still_changes_when_the_clause_text_or_dates_change() -> None:
    base = parse_policy(V2, "x").content_hash
    assert parse_policy(V2.replace("Unchanged.", "Changed."), "x").content_hash != base
    assert parse_policy(V2.replace("2026-03-01", "2026-04-01"), "x").content_hash != base


def test_the_hash_of_a_file_without_metadata_lines_is_the_original_whole_file_hash() -> None:
    assert parse_policy(V1, "x").content_hash == hashlib.sha256(V1.encode()).hexdigest()


def test_published_documents_declare_the_links_and_keep_their_original_hash() -> None:
    docs = {(d.policy_type, d.version): d for d in load_policies()}
    assert docs[("refund", 2)].incorporations == (
        Incorporation(5, ("refund-v1-clause-5", "refund-v1-clause-6")),
    )
    assert docs[("cancellation", 2)].incorporations == (
        Incorporation(
            1, ("cancellation-v1-clause-1", "cancellation-v1-clause-2", "cancellation-v1-clause-3")
        ),
    )
    assert not docs[("refund", 1)].incorporations and not docs[("shipping", 1)].incorporations


def test_the_expanded_corpus_is_between_60_and_100_clauses_and_all_simulated() -> None:
    docs = load_policies()
    total = sum(len(d.clauses) for d in docs)
    assert 60 <= total <= 100, total
    assert {d.policy_type for d in docs} >= {
        "shipping",
        "refund",
        "cancellation",
        "merchant_returns",
        "gift_cards",
        "shipping_restrictions",
        "warranty",
        "faq_archive",
    }
    for d in docs:  # the loader already refuses a document not labelled simulated
        assert d.source_path.startswith("policies/")
    assert {d.audience for d in docs} == {"customer", "merchant"}
    assert {d.status for d in docs} == {"current", "archived"}
