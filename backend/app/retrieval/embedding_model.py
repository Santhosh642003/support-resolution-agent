"""Identity of the local embedding model. Stored alongside every vector so a model change is
detectable instead of silent. The pgvector column dimension (384) follows from this model.

This is the EMBEDDING model, not a chat model; the "no model names outside the adapter
layer" rule applies to chat providers.
"""

from dataclasses import dataclass

EMBEDDING_MODEL_ID = "BAAI/bge-small-en-v1.5"
# Hugging Face commit, verified via the HF API on 2026-11-10.
EMBEDDING_MODEL_REVISION = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
EMBEDDING_DIM = 384
MAX_SEQ_LENGTH = 512  # from sentence_bert_config.json at the pinned revision


@dataclass(frozen=True)
class ModelFile:
    remote_path: str
    size: int
    sha256: str


# SHA-256 of model.onnx is the Hugging Face LFS object id (independent of our download).
# tokenizer.json is not an LFS file; its hash was computed on 2026-11-10 from the pinned
# revision (trust on first use).
MODEL_FILES: tuple[ModelFile, ...] = (
    ModelFile(
        "onnx/model.onnx",
        133093490,
        "828e1496d7fabb79cfa4dcd84fa38625c0d3d21da474a00f08db0f559940cf35",
    ),
    ModelFile(
        "tokenizer.json", 711396, "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66"
    ),
)
