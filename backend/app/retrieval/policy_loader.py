"""Load versioned policy documents from policies/<type>/v<N>.md into clause-level chunks.

Source format: a small front matter block, a banner line, then numbered clauses.

    ---
    policy_type: refund
    version: 2
    effective_from: 2026-03-01
    effective_to: 2026-02-28      (optional, INCLUSIVE; omitted means open-ended)
    title: Refund Policy
    simulated: true
    audience: customer            (optional: customer | merchant; default customer)
    status: current               (optional: current | archived; default current)
    incorporates: 5 <- refund-v1-clause-5, refund-v1-clause-6   (optional, see below)
    ---
    1. First clause text.
    2. Second clause text.

Incorporation. A clause of a later version may declare that it INCORPORATES specific clauses
of an earlier version of the same policy, by stable chunk key (`incorporates: <clause> <- <key>,
<key>; <clause> <- <key>`). Which version is IN EFFECT is still decided only by the validity
dates; an incorporated clause is brought in by the in-effect clause that names it, not because
its own validity matched. One hop only: a target may not itself incorporate anything.

The content hash covers what a citation depends on (identity, dates, title, clauses). The
metadata-only front matter lines (audience, status, incorporates) are excluded from it, so adding
or declaring them never changes the hash of an already-published document; ingestion verifies
them separately.

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

POLICY_TYPES = (
    "shipping",
    "refund",
    "cancellation",
    "merchant_returns",
    "gift_cards",
    "shipping_restrictions",
    "warranty",
    "faq_archive",
)
AUDIENCES = ("customer", "merchant")
STATUSES = ("current", "archived")
# Front matter keys that do not enter the content hash.
_METADATA_ONLY = ("audience", "status", "incorporates")
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
class Incorporation:
    """Clause `clause_number` of this document incorporates the clauses `targets` (chunk keys of
    an earlier version of the same policy)."""

    clause_number: int
    targets: tuple[str, ...]


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
    audience: str = "customer"
    status: str = "current"
    incorporations: tuple[Incorporation, ...] = ()

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


def parse_incorporations(value: str, source_path: str) -> tuple[Incorporation, ...]:
    """`5 <- refund-v1-clause-5, refund-v1-clause-6; 2 <- refund-v1-clause-3`."""
    out: list[Incorporation] = []
    for part in (p.strip() for p in value.split(";") if p.strip()):
        left, arrow, right = part.partition("<-")
        keys = tuple(k.strip() for k in right.split(",") if k.strip())
        if not arrow or not left.strip().isdigit() or not keys:
            raise PolicyFormatError(f"{source_path}: bad incorporates entry {part!r}")
        if len(set(keys)) != len(keys):
            raise PolicyFormatError(f"{source_path}: duplicate incorporation target in {part!r}")
        out.append(Incorporation(int(left.strip()), keys))
    numbers = [i.clause_number for i in out]
    if len(set(numbers)) != len(numbers):
        raise PolicyFormatError(f"{source_path}: a clause may be listed only once in incorporates")
    return tuple(out)


def _content_hash(lines: list[str], end: int) -> str:
    """sha256 of the normalized file text, minus metadata-only front matter lines. For a file
    without those lines this equals sha256 of the whole normalized text (the original
    definition)."""
    kept = [
        line
        for i, line in enumerate(lines)
        if not (0 < i < end and line.partition(":")[0].strip() in _METADATA_ONLY)
    ]
    return hashlib.sha256("\n".join(kept).encode("utf-8")).hexdigest()


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
    audience = meta.get("audience", "customer")
    status = meta.get("status", "current")
    if audience not in AUDIENCES:
        raise PolicyFormatError(f"{source_path}: audience must be one of {AUDIENCES}")
    if status not in STATUSES:
        raise PolicyFormatError(f"{source_path}: status must be one of {STATUSES}")
    incorporations = parse_incorporations(meta.get("incorporates", ""), source_path)

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
        content_hash=_content_hash(lines, end),
        clauses=tuple(clauses),
        audience=audience,
        status=status,
        incorporations=incorporations,
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
    _validate_incorporations(by_type)


def _validate_incorporations(by_type: dict[str, list[PolicyDocument]]) -> None:
    """Every declared link must name a real clause of an EARLIER version of the SAME policy, and
    the graph is one hop deep: no target may itself incorporate, no source may be a target."""
    known: dict[str, PolicyDocument] = {}
    for versions in by_type.values():
        for d in versions:
            for c in d.clauses:
                known[d.chunk_key(c.number)] = d
    sources: set[str] = set()
    targets: set[str] = set()
    for versions in by_type.values():
        for d in versions:
            for inc in d.incorporations:
                if inc.clause_number not in {c.number for c in d.clauses}:
                    raise PolicyFormatError(
                        f"{d.source_path}: incorporates names clause {inc.clause_number}, "
                        "which does not exist"
                    )
                src = d.chunk_key(inc.clause_number)
                sources.add(src)
                for t in inc.targets:
                    target_doc = known.get(t)
                    if target_doc is None:
                        raise PolicyFormatError(f"{src}: incorporates unknown clause {t!r}")
                    if target_doc.policy_type != d.policy_type:
                        raise PolicyFormatError(
                            f"{src}: may only incorporate the same policy, not {t}"
                        )
                    if target_doc.version >= d.version:
                        raise PolicyFormatError(
                            f"{src}: may only incorporate an EARLIER version, not {t}"
                        )
                    targets.add(t)
    chained = sources & targets
    if chained:
        raise PolicyFormatError(
            "incorporation chains are not supported (one hop only); "
            f"chained clauses: {sorted(chained)}"
        )


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
