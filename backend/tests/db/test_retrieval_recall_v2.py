"""Round 2 of the retrieval measurement (tuned full-text leg + structured scoping), recorded in
docs/retrieval/recall_v2.json. The numbers reproduce exactly, and the findings that matter are
pinned so they cannot be quietly rewritten into something tidier.

Regenerate deliberately with `uv run python -m tests.retrieval_eval2` and review the diff."""

import json

import pytest
from sqlalchemy import Connection

from app.retrieval.embedder import OnnxBgeEmbedder
from app.retrieval.ingest import ingest_policies
from app.retrieval.policy_loader import load_policies
from app.seed.loader import seed_all
from tests.retrieval_eval2 import RESULTS, measure


@pytest.fixture(scope="module")
def embedder() -> OnnxBgeEmbedder:
    return OnnxBgeEmbedder()


def recorded() -> dict:  # type: ignore[type-arg]
    return json.loads(RESULTS.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def test_the_recorded_round_2_numbers_reproduce_exactly(
    conn: Connection, embedder: OnnxBgeEmbedder
) -> None:
    seed_all(conn)
    ingest_policies(conn, embedder, load_policies())
    fresh = measure(conn, embedder)
    rec = recorded()
    fresh.pop("protocol", None)
    rec.pop("protocol", None)
    assert fresh == rec


def test_the_pre_registered_decisions_and_what_they_chose() -> None:
    r = recorded()
    assert r["R1"]["fts_only_full_recall_k7_selection"] == {"A": 6, "B": 13, "C": 13, "D": 5}
    assert r["R1"]["chosen"] == "B"  # a tie with C goes to the first listed
    assert r["R3"]["rule_met"] is True and r["R3"]["chosen_k"] == 7
    assert "THIS IS THE LAST TUNING PASS" in r["protocol"]  # the contamination note travels with it


def test_tuning_the_lexical_leg_was_worth_it_it_was_not_a_straw_man_win() -> None:
    g = recorded()["stage1_lexical_grid_S3"]

    def sel(n: str, split: str) -> dict[str, int]:
        return g[n][split]["full_recall"]["fts_only"]  # type: ignore[no-any-return]

    for split in ("selection", "heldout"):
        for k in range(3, 11):
            assert sel("B", split)[str(k)] >= sel("D", split)[str(k)], (split, k)
    assert sel("B", "selection")["7"] - sel("D", "selection")["7"] == 8  # 13 against 5
    assert sel("B", "heldout")["7"] - sel("D", "heldout")["7"] == 9  # 22 against 13


def test_scoping_removes_most_non_core_slots_and_never_loses_a_needed_clause() -> None:
    r = recorded()
    slots = r["stage2_scoping"]["noncore_slots_hybrid_w1.0_k7"]
    assert slots["S3_heldout"]["non_core"] / slots["S3_heldout"]["slots"] > 0.45  # about half
    assert slots["S4_heldout"]["non_core"] / slots["S4_heldout"]["slots"] < 0.20
    d = r["diagnostics"]
    assert (
        d["scope_coverage"]["needed_clauses_inside_scope"] == d["scope_coverage"]["needed_clauses"]
    )
    for split in ("selection", "heldout"):
        assert d["missed_clause_attribution_at_final_config"][split]["scoping"] == 0
        assert d["missed_clause_attribution_at_final_config"][split]["date"] == 0


def test_the_registered_fusion_rule_decided_on_a_saturated_comparison() -> None:
    """The record of a weakness in the protocol, not of the retrieval: at k=7 every candidate hits
    16/16 on the selection set, so 'strictly beats vector-only' could not discriminate and fell
    through to the tie-break. Where the comparison is NOT saturated (k=3..6), hybrid is at least
    as good as vector-only on the selection set at every k."""
    r = recorded()
    assert r["R2"]["adopted"] == "vector_only"  # as registered, by the tie-break
    scores = r["R2"]["selection_full_recall_k7"]
    assert scores["vector_only"] == scores["hybrid_w1.0"] == scores["hybrid_w0.5"] == 16
    sel = r["stage2_scoping"]["S4_structured_scope"]["selection"]["full_recall"]
    for k in range(3, 7):
        assert sel["hybrid_w1.0"][str(k)] >= sel["vector_only"][str(k)], k
        assert sel["fts_only"][str(k)] >= sel["vector_only"][str(k)], k


def test_the_numeric_token_probe_does_not_discriminate_between_the_legs() -> None:
    """The hypothesis that a lexical leg wins where an exact token is present was NOT supported:
    queries that contain the token are found by every leg, including the original untuned one."""
    found = recorded()["diagnostics"]["numeric_probe"]["found_in_top_k"]
    for k, row in found.items():
        assert set(row.values()) == {10}, (k, row)


def test_the_gift_card_scope_is_deliberate_and_its_cost_is_visible_in_the_trace() -> None:
    g = recorded()["diagnostics"]["gift_card_scoping"]
    assert g["order_with_gift_card_full_recall"] == g["queries"] == 4
    assert g["same_queries_on_an_order_without_one_full_recall"] == 0  # scoped out by the facts...
    assert g["scope_trace_reason_when_excluded"] == [
        "the order contains no gift card item"
    ]  # ...and traced
