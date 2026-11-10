"""Load versioned policy documents from policies/<type>/v<N>.md into clause-level chunks.

Source format: a small front matter block, a banner line, then numbered clauses.

    ---
    policy_type: refund
    version: 2
    effective_from: 2026-03-01
    effective_to: 2026-02-28      (optional, INCLUSIVE; omitted means open-ended)
    title: Refund Policy
    simulated: true
    ---
    1. First clause text.
    2. Second clause text.

Chunk identity: clause numbers are written explicitly in the source, and the chunk key is
`<policy_type>-v<version>-clause-<number>` (for example `refund-v2-clause-3`). A citation
therefore reads "Refund Policy v2, clause 3", which a human can check, and the key survives
re-ingestion because it never depends on position in the database.
"""

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

POLICY_TYPES = ("shipping", "refund", "cancellation")
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_POLICIES_DIR = REPO_ROOT / "policies"

_CLAUSE = re.compile(r"^(\d+)\.\s+(.*\S)\s*$")
_REQUIRED = ("policy_type", "version", "effective_from", "title", "simulated")


class PolicyFormatError(ValueError):
    pass


@dataclass(frozen=True)
class Clause:
    number: int
    text: str


@dataclass(frozen=True)
class PolicyDocument:
    policy_type: str
    version: int
    effective_from: date
    effective_to: date | None  # inclusive, as written in the policy text
    title: str
    source_path: str
    content_hash: str
    clauses: tuple[Clause, ...]

    @property
    def valid_from(self) -> date:
        return self.effective_from

    @property
    def valid_to_exclusive(self) -> date | None:
        """Upper bound for the daterange '[)'; the inclusive end date plus one day."""
        return None if self.effective_to is None else self.effective_to + timedelta(days=1)

    def chunk_key(self, clause_number: int) -> str:
        return chunk_key(self.policy_type, self.version, clause_number)

    def heading(self, clause_number: int) -> str:
        return f"{self.title} v{self.version}, clause {clause_number}"

    def embedding_text(self, clause: Clause) -> str:
        """What is embedded: the clause with its human-readable citation label as context."""
        return f"{self.heading(clause.number)}: {clause.text}"


def chunk_key(policy_type: str, version: int, clause_number: int) -> str:
    return f"{policy_type}-v{version}-clause-{clause_number}"


def parse_policy(text: str, source_path: str) -> PolicyDocument:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if not lines or lines[0].strip() != "---":
        raise PolicyFormatError(f"{source_path}: must start with a '---' front matter block")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise PolicyFormatError(f"{source_path}: front matter is not closed with '---'") from exc

    meta: dict[str, str] = {}
    for raw in lines[1:end]:
        if not raw.strip():
            continue
        if ":" not in raw:
            raise PolicyFormatError(f"{source_path}: bad front matter line {raw!r}")
        key, _, value = raw.partition(":")
        meta[key.strip()] = value.strip()
    missing = [k for k in _REQUIRED if k not in meta]
    if missing:
        raise PolicyFormatError(f"{source_path}: missing front matter keys {missing}")
    if meta["simulated"].lower() != "true":
        raise PolicyFormatError(f"{source_path}: policies must be labeled simulated: true")
    if meta["policy_type"] not in POLICY_TYPES:
        raise PolicyFormatError(f"{source_path}: unknown policy_type {meta['policy_type']!r}")

    try:
        version = int(meta["version"])
        effective_from = date.fromisoformat(meta["effective_from"])
        effective_to = date.fromisoformat(meta["effective_to"]) if "effective_to" in meta else None
    except ValueError as exc:
        raise PolicyFormatError(f"{source_path}: bad version or date: {exc}") from exc
    if version < 1:
        raise PolicyFormatError(f"{source_path}: version must be >= 1")
    if effective_to is not None and effective_to < effective_from:
        raise PolicyFormatError(f"{source_path}: effective_to is before effective_from")

    clauses: list[Clause] = []
    for raw in lines[end + 1 :]:
        match = _CLAUSE.match(raw)
        if match:
            clauses.append(Clause(int(match.group(1)), match.group(2)))
        elif raw.startswith("   ") and raw.strip() and clauses:
            last = clauses[-1]  # indented continuation of the previous clause
            clauses[-1] = Clause(last.number, f"{last.text} {raw.strip()}")
    if not clauses:
        raise PolicyFormatError(f"{source_path}: no numbered clauses found")
    if [c.number for c in clauses] != list(range(1, len(clauses) + 1)):
        raise PolicyFormatError(
            f"{source_path}: clause numbers must be explicit, unique and run 1..N in order, "
            f"got {[c.number for c in clauses]}"
        )

    return PolicyDocument(
        policy_type=meta["policy_type"],
        version=version,
        effective_from=effective_from,
        effective_to=effective_to,
        title=meta["title"],
        source_path=source_path,
        content_hash=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        clauses=tuple(clauses),
    )


def validate_corpus(docs: Iterable[PolicyDocument]) -> None:
    """Versions of one policy are numbered 1..N, adjacent in time (no gap, no overlap), and
    only the last is open-ended. The database also forbids overlap (exclusion constraint)."""
    by_type: dict[str, list[PolicyDocument]] = {}
    for doc in docs:
        by_type.setdefault(doc.policy_type, []).append(doc)
    for policy_type, versions in by_type.items():
        versions.sort(key=lambda d: d.version)
        if [d.version for d in versions] != list(range(1, len(versions) + 1)):
            raise PolicyFormatError(
                f"{policy_type}: versions must be 1..N, got {[d.version for d in versions]}"
            )
        for earlier, later in zip(versions, versions[1:], strict=False):
            if earlier.valid_to_exclusive is None:
                raise PolicyFormatError(
                    f"{policy_type} v{earlier.version} is open-ended but v{later.version} exists"
                )
            if earlier.valid_to_exclusive != later.valid_from:
                raise PolicyFormatError(
                    f"{policy_type}: v{earlier.version} ends {earlier.effective_to} but "
                    f"v{later.version} starts {later.valid_from} (gap or overlap)"
                )
        if versions[-1].valid_to_exclusive is not None:
            raise PolicyFormatError(f"{policy_type}: the latest version must be open-ended")


def load_policies(policies_dir: Path = DEFAULT_POLICIES_DIR) -> list[PolicyDocument]:
    docs: list[PolicyDocument] = []
    for path in sorted(policies_dir.glob("*/v*.md")):
        rel = path.relative_to(policies_dir.parent).as_posix()
        doc = parse_policy(path.read_text(encoding="utf-8"), rel)
        expected = f"{doc.policy_type}/v{doc.version}.md"
        if path.relative_to(policies_dir).as_posix() != expected:
            raise PolicyFormatError(f"{rel}: path does not match its front matter ({expected})")
        docs.append(doc)
    if not docs:
        raise PolicyFormatError(f"no policies found under {policies_dir}")
    validate_corpus(docs)
    return docs
