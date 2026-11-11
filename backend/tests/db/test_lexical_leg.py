"""The full-text leg's configurations (SPEC 7.3). Every form keeps the date filter inside its WHERE
clause; numeric tokens survive the text search configuration; AND-then-OR behaves as described."""

from datetime import date

import pytest
from sqlalchemy import Connection, text

from app.domain.types import PurchaseDate
from app.retrieval.embedder import OnnxBgeEmbedder
from app.retrieval.hybrid import BASELINE_LEXICAL, LexicalConfig, fts_leg
from app.retrieval.ingest import ingest_policies
from app.retrieval.policy_loader import load_policies

V1_DAY = PurchaseDate(date(2026, 2, 15))
V2_DAY = PurchaseDate(date(2026, 4, 1))
TYPES = ("refund", "cancellation", "shipping", "gift_cards", "warranty")

FORMS = [
    LexicalConfig("or", "rank", False),
    LexicalConfig("and", "rank_cd", True),
    LexicalConfig("and_then_or", "rank_cd", True),
    LexicalConfig("or", "rank_cd", True),
]


@pytest.fixture(scope="module")
def embedder() -> OnnxBgeEmbedder:
    return OnnxBgeEmbedder()


@pytest.fixture
def corpus(conn: Connection, embedder: OnnxBgeEmbedder) -> Connection:
    ingest_policies(conn, embedder, load_policies())
    return conn


def keys(conn: Connection, q: str, day: PurchaseDate, cfg: LexicalConfig, n: int = 20) -> list[str]:
    return [c.chunk_key for c in fts_leg(conn, q, day, TYPES, n, lexical=cfg)]


@pytest.mark.parametrize("cfg", FORMS)
def test_every_form_applies_the_date_filter_inside_the_query(
    corpus: Connection, cfg: LexicalConfig
) -> None:
    v1_wording = "Refund window: 30 days from delivery date. Auto-approve ceiling: 7500 cents."
    v2_wording = "Refund window extended to 45 days. Auto-approve ceiling raised to 12000 cents."
    for q, day, wrong in ((v2_wording, V1_DAY, "v2"), (v1_wording, V2_DAY, "v1")):
        got = keys(corpus, q, day, cfg)
        # AND needs every term in one clause, so quoting the OTHER version's words can match nothing
        assert got or cfg.form == "and", (cfg, q)
        for key in got:
            if key.startswith(("refund-", "cancellation-", "gift_cards-", "warranty-")):
                assert f"-{wrong}-" not in key, (cfg, key)


def test_numeric_tokens_survive_the_text_search_configuration(corpus: Connection) -> None:
    cfg = LexicalConfig("and", "rank_cd", True)
    assert keys(corpus, "ceiling 7500 cents", V1_DAY, cfg)[0] == "refund-v1-clause-3"
    assert keys(corpus, "refund window 45 days", V2_DAY, cfg)[0] == "refund-v2-clause-1"
    assert keys(corpus, "raised to 12000 cents", V2_DAY, cfg)[0] == "refund-v2-clause-2"
    # and the tokens really are in the stored vectors
    tokens = corpus.execute(
        text("SELECT tsv::text FROM policy_chunks WHERE chunk_key = 'refund-v2-clause-1'")
    ).scalar_one()
    assert "'45'" in tokens


def test_and_requires_every_term_and_or_does_not(corpus: Connection) -> None:
    chatty = "hello there my parrot ate my homework but also the refund window please"
    assert keys(corpus, chatty, V2_DAY, LexicalConfig("and", "rank_cd", True)) == []
    assert "refund-v2-clause-1" in keys(
        corpus, chatty, V2_DAY, LexicalConfig("or", "rank_cd", True)
    )


def test_and_then_or_puts_strict_matches_first_without_duplicates_and_within_n(
    corpus: Connection,
) -> None:
    q = "refund window days parrot"
    strict = keys(corpus, q, V2_DAY, LexicalConfig("and", "rank_cd", True))
    loose = keys(corpus, q, V2_DAY, LexicalConfig("or", "rank_cd", True))
    both = keys(corpus, q, V2_DAY, LexicalConfig("and_then_or", "rank_cd", True), n=5)
    assert strict == []  # 'parrot' matches nothing, so AND finds nothing...
    assert both == loose[:5]  # ...and AND-then-OR falls back to the OR ranking
    q2 = "refund window days"
    strict2 = keys(corpus, q2, V2_DAY, LexicalConfig("and", "rank_cd", True))
    both2 = keys(corpus, q2, V2_DAY, LexicalConfig("and_then_or", "rank_cd", True), n=8)
    assert both2[: len(strict2)] == strict2 and len(both2) == len(set(both2)) and len(both2) <= 8


def test_the_title_weight_lifts_a_policy_whose_title_matches(corpus: Connection) -> None:
    weighted = keys(corpus, "store credit policy", V2_DAY, LexicalConfig("or", "rank_cd", True), 3)
    assert weighted and all(k.startswith("gift_cards-") for k in weighted)


def test_the_baseline_is_the_original_untuned_leg_and_the_default_argument() -> None:
    assert BASELINE_LEXICAL == LexicalConfig("or", "rank", False)
    import inspect

    assert inspect.signature(fts_leg).parameters["lexical"].default == BASELINE_LEXICAL


@pytest.mark.parametrize("cfg", FORMS)
def test_results_are_deterministic(corpus: Connection, cfg: LexicalConfig) -> None:
    q = "I want a refund for the damaged box within the window"
    assert keys(corpus, q, V2_DAY, cfg) == keys(corpus, q, V2_DAY, cfg)
