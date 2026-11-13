"""The recorded recall numbers (docs/retrieval/recall.json) are reproducible, and the protocol that
produced them could not have let the held-out set influence anything.

If this fails because the corpus, the embedder or the retrieval code changed, regenerate with
`uv run python -m tests.retrieval_eval` and review the diff: a changed number is a changed result.
"""

import json

import pytest
from sqlalchemy import Connection

from app.retrieval.embedder import OnnxBgeEmbedder
from app.retrieval.ingest import ingest_policies
from app.retrieval.policy_loader import load_policies
from tests.fixtures.retrieval_cases import CASES
from tests.fixtures.retrieval_heldout import HELDOUT_QUERIES
from tests.retrieval_eval import FROZEN_POLICIES, RESULTS, all_queries, measure


@pytest.fixture(scope="module")
def embedder() -> OnnxBgeEmbedder:
    return OnnxBgeEmbedder()


def test_the_recorded_recall_numbers_reproduce_exactly(
    conn: Connection, embedder: OnnxBgeEmbedder
) -> None:
    ingest_policies(conn, embedder, load_policies(FROZEN_POLICIES))
    default_k = 7  # the k in force when the round-1 numbers were recorded; not today's default
    fresh = measure(conn, embedder, default_k)
    recorded = json.loads(RESULTS.read_text(encoding="utf-8"))
    fresh.pop("protocol", None)
    recorded.pop("protocol", None)
    assert fresh == recorded


def test_the_selection_and_heldout_sets_are_disjoint_and_sized_as_reported() -> None:
    queries = all_queries()
    selection = {q.text for q in queries if q.split == "selection"}
    held = {q.text for q in queries if q.split == "heldout"}
    assert len(selection) == 16 and len(held) == 32
    assert not selection & held
    assert set(HELDOUT_QUERIES) == {c.case_key for c in CASES}  # every case is covered, 4 each
    assert all(len(v) == 4 for v in HELDOUT_QUERIES.values())


def test_the_chosen_k_comes_from_the_selection_set_only(
    conn: Connection, embedder: OnnxBgeEmbedder
) -> None:
    """Recomputing the choice from selection numbers alone gives the recorded k, and the rule is
    recorded with it. (k is unchanged because no k <= 10 met the rule on the selection set.)"""
    from tests.retrieval_eval import choose_k

    recorded = json.loads(RESULTS.read_text(encoding="utf-8"))
    assert choose_k(recorded["S0_selection"], 7) == recorded["chosen_k"]
    best = max(c["full"] for c in recorded["S0_selection"]["modes"]["hybrid"].values())
    assert (
        best < 15
    )  # the rule was NOT met, so 7 is the unchanged default, not an evidence-chosen k


def test_the_hybrid_beats_random_but_not_its_best_leg_and_the_record_says_so() -> None:
    """The honest result, pinned so it cannot be quietly rewritten as a win."""
    r = json.loads(RESULTS.read_text(encoding="utf-8"))
    for split in ("S0_selection", "S0_heldout"):
        t = r[split]
        for k in range(3, 11):
            hybrid = t["modes"]["hybrid"][str(k)]["full"]
            assert hybrid > t["random"][str(k)]["full"], (split, k)  # clearly above chance
            assert t["modes"]["vector_only"][str(k)]["full"] >= hybrid, (
                split,
                k,
            )  # ...not above vector
    assert r["weight_sweep"]["chosen_fts_weight_on_selection"] == 0.0
    assert r["weight_sweep"]["heldout_at_chosen"] > r["weight_sweep"]["heldout_at_equal_weights"]
