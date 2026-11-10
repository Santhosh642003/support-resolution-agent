"""Local CPU embedder for bge-small-en-v1.5 using ONNX Runtime, with no torch.

Reproduces what sentence-transformers does for this model (modules.json at the pinned
revision): Transformer (max 512 tokens) -> CLS-token pooling -> L2 normalisation. No query
instruction prefix is applied. Equivalence is gated by tests/unit/test_embedding_parity.py
against reference vectors committed from sentence-transformers.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

import numpy as np
import onnxruntime as ort
from numpy.typing import NDArray
from tokenizers import Tokenizer

from app.retrieval.embedding_model import (
    EMBEDDING_DIM,
    EMBEDDING_MODEL_ID,
    EMBEDDING_MODEL_REVISION,
    MAX_SEQ_LENGTH,
    MODEL_FILES,
)
from app.retrieval.fetch_model import DEFAULT_MODEL_DIR, sha256_of

Vectors = NDArray[np.float32]


class Embedder(Protocol):
    model_id: str
    revision: str
    dim: int

    def embed(self, texts: Sequence[str]) -> Vectors: ...


class ModelFilesMissingError(RuntimeError):
    pass


class OnnxBgeEmbedder:
    model_id = EMBEDDING_MODEL_ID
    revision = EMBEDDING_MODEL_REVISION
    dim = EMBEDDING_DIM

    def __init__(
        self,
        model_dir: Path = DEFAULT_MODEL_DIR,
        *,
        intra_op_threads: int = 2,
        batch_size: int = 16,
        verify_checksums: bool = True,
    ) -> None:
        self._batch_size = batch_size
        paths = {
            Path(f.remote_path).name: model_dir / Path(f.remote_path).name for f in MODEL_FILES
        }
        for spec in MODEL_FILES:
            path = paths[Path(spec.remote_path).name]
            if not path.is_file():
                raise ModelFilesMissingError(
                    f"{path} is missing. Run `uv run python -m app.retrieval.fetch_model`."
                )
            if verify_checksums and sha256_of(path) != spec.sha256:
                raise ModelFilesMissingError(f"{path} does not match the pinned SHA-256")

        self._tokenizer = Tokenizer.from_file(str(paths["tokenizer.json"]))
        self._tokenizer.enable_truncation(max_length=MAX_SEQ_LENGTH)
        pad_id = self._tokenizer.token_to_id("[PAD]")
        self._tokenizer.enable_padding(pad_id=0 if pad_id is None else pad_id, pad_token="[PAD]")  # noqa: S106

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = intra_op_threads
        opts.inter_op_num_threads = 1
        self._session = ort.InferenceSession(
            str(paths["model.onnx"]), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self._input_names = {i.name for i in self._session.get_inputs()}

    def embed(self, texts: Sequence[str]) -> Vectors:
        """Return L2-normalised float32 vectors, shape (len(texts), 384), in input order."""
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        # Sort by length to limit padding, then restore the caller's order.
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for start in range(0, len(order), self._batch_size):
            idx = order[start : start + self._batch_size]
            out[idx] = self._embed_batch([texts[i] for i in idx])
        return out

    def _embed_batch(self, batch: Sequence[str]) -> Vectors:
        encodings = self._tokenizer.encode_batch(list(batch))
        feeds: dict[str, NDArray[np.int64]] = {
            "input_ids": np.array([e.ids for e in encodings], dtype=np.int64),
            "attention_mask": np.array([e.attention_mask for e in encodings], dtype=np.int64),
        }
        if "token_type_ids" in self._input_names:
            feeds["token_type_ids"] = np.array([e.type_ids for e in encodings], dtype=np.int64)
        hidden = self._session.run(None, feeds)[0]  # (batch, seq, 384) last_hidden_state
        cls = np.asarray(hidden[:, 0, :], dtype=np.float32)  # CLS pooling
        norm = np.linalg.norm(cls, axis=1, keepdims=True)
        return np.asarray(cls / np.maximum(norm, 1e-12), dtype=np.float32)
