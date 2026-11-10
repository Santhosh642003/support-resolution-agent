"""Blocking gate: the ONNX embedder must match sentence-transformers reference vectors.

Reference vectors were generated ONCE in a throwaway environment
(scripts/generate_embedding_parity_fixture.py) and committed. This test imports no torch and
makes no network calls. It needs the pinned model files on disk
(`uv run python -m app.retrieval.fetch_model`); if they are missing it FAILS, never skips.
"""

import json
from pathlib import Path

import numpy as np
import pytest

from app.retrieval.embedder import OnnxBgeEmbedder
from app.retrieval.embedding_model import (
    EMBEDDING_DIM,
    EMBEDDING_MODEL_ID,
    EMBEDDING_MODEL_REVISION,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "embedding_parity.json"
THRESHOLD = 0.999


@pytest.fixture(scope="module")
def fixture() -> dict[str, object]:
    data: dict[str, object] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


@pytest.fixture(scope="module")
def embedder() -> OnnxBgeEmbedder:
    return OnnxBgeEmbedder()


def test_fixture_records_its_provenance_and_matches_the_pinned_model(
    fixture: dict[str, object],
) -> None:
    meta = fixture["meta"]
    assert isinstance(meta, dict)
    assert meta["model"] == EMBEDDING_MODEL_ID
    assert meta["revision"] == EMBEDDING_MODEL_REVISION
    assert meta["dim"] == EMBEDDING_DIM
    assert meta["sentence_transformers_version"]
    assert meta["torch_version"] and meta["transformers_version"] and meta["generated"]


def test_fixture_covers_required_input_categories(fixture: dict[str, object]) -> None:
    items = fixture["items"]
    assert isinstance(items, list)
    assert len(items) >= 30
    tags = {i["tag"] for i in items}
    assert {"policy", "customer", "adversarial", "unicode", "edge"} <= tags
    assert sum(1 for i in items if i["tag"] == "policy") >= 9  # real (simulated) policy chunks


def test_onnx_matches_sentence_transformers_cosine_at_least_0_999(
    fixture: dict[str, object], embedder: OnnxBgeEmbedder
) -> None:
    items = fixture["items"]
    assert isinstance(items, list)
    ours = embedder.embed([i["text"] for i in items])
    ref = np.array([i["embedding"] for i in items], dtype=np.float32)
    assert ours.shape == ref.shape == (len(items), EMBEDDING_DIM)
    cosine = np.sum(ours * ref, axis=1) / (
        np.linalg.norm(ours, axis=1) * np.linalg.norm(ref, axis=1)
    )
    worst = int(np.argmin(cosine))
    assert cosine[worst] >= THRESHOLD, (
        f"worst cosine {cosine[worst]:.6f} on item {worst} ({items[worst]['tag']}): "
        f"{items[worst]['text'][:60]!r}"
    )


def test_output_is_unit_length_and_independent_of_batching(
    fixture: dict[str, object], embedder: OnnxBgeEmbedder
) -> None:
    items = fixture["items"]
    assert isinstance(items, list)
    texts = [i["text"] for i in items][:12]
    batched = embedder.embed(texts)
    singles = np.vstack([embedder.embed([t]) for t in texts])
    assert np.allclose(np.linalg.norm(batched, axis=1), 1.0, atol=1e-4)
    # Padding in a batch must not change a text's vector.
    assert np.min(np.sum(batched * singles, axis=1)) >= 0.9999


def test_empty_input_returns_empty_matrix(embedder: OnnxBgeEmbedder) -> None:
    assert embedder.embed([]).shape == (0, EMBEDDING_DIM)
