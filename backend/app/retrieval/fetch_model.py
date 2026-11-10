"""Download the pinned embedding model files and verify their SHA-256.

    uv run python -m app.retrieval.fetch_model

Run by developers and as a CI/Docker setup step. The test suite itself never touches the
network; it only reads files this script placed on disk.
"""

import hashlib
import sys
import urllib.request
from pathlib import Path

from app.retrieval.embedding_model import (
    EMBEDDING_MODEL_ID,
    EMBEDDING_MODEL_REVISION,
    MODEL_FILES,
    ModelFile,
)

DEFAULT_MODEL_DIR = Path(__file__).resolve().parents[2] / "models" / "bge-small-en-v1.5"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _verify(path: Path, spec: ModelFile) -> bool:
    return path.is_file() and path.stat().st_size == spec.size and sha256_of(path) == spec.sha256


def fetch(model_dir: Path = DEFAULT_MODEL_DIR) -> None:
    for spec in MODEL_FILES:
        dest = model_dir / Path(spec.remote_path).name
        if _verify(dest, spec):
            print(f"ok (cached) {dest.name}")
            continue
        url = (
            f"https://huggingface.co/{EMBEDDING_MODEL_ID}/resolve/"
            f"{EMBEDDING_MODEL_REVISION}/{spec.remote_path}"
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        print(f"downloading {url}")
        urllib.request.urlretrieve(url, tmp)  # noqa: S310 (fixed https URL)
        if not _verify(tmp, spec):
            actual = sha256_of(tmp)
            tmp.unlink()
            raise SystemExit(f"checksum/size mismatch for {spec.remote_path}: got sha256 {actual}")
        tmp.replace(dest)
        print(f"ok {dest.name}")


if __name__ == "__main__":
    fetch(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_MODEL_DIR)
