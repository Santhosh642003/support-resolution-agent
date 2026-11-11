# CLAUDE.md

Guidance for anyone (human or AI) working in this repository. Read fully before making changes.

## Project summary

A customer-support resolution agent for a **fictional subscription-commerce company**. It is a portfolio project: correctness under hard conditions, measured evaluation, and honest documentation of failures matter more than feature count. **All data is synthetic and must be labeled as simulated** (in the UI, docs, seed data, and reports).

The agent answers policy questions, reads live order and subscription data through APIs, and performs tightly controlled actions. Version 1 has exactly three workflows:

1. Order-status lookup
2. Subscription cancellation
3. Refund requests

Example request: "My replacement order hasn't arrived, cancel my subscription and refund the missing shipment." The system must: authenticate the customer; retrieve the applicable shipping, refund, and cancellation policies; fetch current order and subscription records via APIs; decide permitted actions with explicit business rules in code; present proposed changes for customer confirmation; execute permitted actions exactly once even under retries; return a receipt or hand off to a human with the evidence gathered.

Full design: `docs/SPEC.md`.

## Non-negotiable design rules

- **Deterministic action checks.** Code decides eligibility and amounts. The model only explains decisions.
- **Server-side authorization.** Customer identity comes from the authenticated session. The model can never supply or choose a customer ID.
- **Idempotency.** Repeated or retried requests cannot produce duplicate refunds or cancellations.
- **Live data.** Order and subscription state comes from the API at request time, never from embeddings.
- **Policy versioning.** Policies have effective dates and versions; the applicable policy can depend on purchase date. Answers cite the policy text used.
- **Failure honesty.** API timeouts produce an explicit pending or failed state, never a claimed success.
- **Injection resistance.** Retrieved documents and customer messages cannot grant permissions or override backend rules.
- **Human handoff.** Unresolved cases carry the relevant evidence and the actions attempted.
- **Bounded workflow.** An explicit state machine with a capped number of tool calls.
- **Citations come from the Decision.** The rules engine emits, with every Decision, the exact clause keys it applied; those are the citations. Retrieval supplies context text and answers general policy questions; it cannot add, remove or change a citation, and a similarity score never influences an outcome. The model never selects citations (SPEC 10.4).

Release bar: **zero unauthorized writes and zero duplicate refunds** in the defined test suite.

## Stack

- Backend: Python, FastAPI, typed request/response schemas
- Data: PostgreSQL (customers, orders, subscriptions, actions, audit records)
- Retrieval: PostgreSQL full-text search + pgvector with rank fusion; reranker optional later
- Frontend: React chat, confirmation panel, customer account view, handoff queue
- Infra: Docker, database migrations, GitHub Actions CI, one cloud deployment later (OCI Always Free, Ampere A1 / arm64)
- Observability: retrieval results, tool calls, failures, token usage, latency, cost per case

## Out of scope for V1

Voice, mobile, fine-tuning, graph databases, multiple agents, extra connectors, and any workflow beyond the three above.

## Standing constraints

### Model access: free tier only
No paid API access. This is a hard constraint, and the architecture must keep it invisible to the rest of the codebase.

- **Provider abstraction.** One internal interface for chat completion and structured output. Adapters: `GroqAdapter` (primary, OpenAI-compatible, `base_url` swap), `GeminiAdapter` (secondary), `FakeAdapter` (deterministic, no network). Provider and model are selected by environment variable.
- **No vendor SDK outside the adapter layer.** No module outside `app/providers/` may import a vendor SDK or reference a model name. Enforce with a test/lint check.
- **Rate limiting is a first-class feature.** Token-bucket limiter, bounded exponential backoff with jitter, capped retries, and a clearly surfaced daily-quota-exhausted error. Quota failures map to the workflow's pending/failed path, never a fabricated success.
- **Response cache** in PostgreSQL keyed by hash of (provider, model, full prompt, temperature, tool schema version). Eval runs replay from cache at zero calls with identical results. Cache hits/misses appear in the trace; eval reports include cache-hit rate.
- **Cost accounting** still applies: tokens per case, latency split into model time vs tool time, configurable price-per-token. Label cost figures **projected, not incurred**.

### No network in CI
All unit tests, all CI runs, and all business-rule tests pass with zero network calls using `FakeAdapter`. CI must never require an API key. Tests that need the network are marked and excluded from CI.

### Local embeddings
Local CPU embeddings with **ONNX Runtime + tokenizers + numpy** (no torch in project dependencies, CLS pooling and L2 normalisation hand-implemented). Model `BAAI/bge-small-en-v1.5`, revision `5c38ec7c405ec4b44b94cc5a9bb96e735b38267a` (verified 2026-11-10), 384 dimensions. No embedding API. A parity test against committed sentence-transformers reference vectors (cosine >= 0.999, runs with no torch and no network) is a blocking gate for retrieval work. The gate has passed (min cosine 0.9999999). Model files are not committed; fetch them with `uv run python -m app.retrieval.fetch_model` (SHA-256 verified). Pin model name and revision; store the embedding model identifier alongside every stored vector so a model change is detectable, not silent; record the resulting pgvector dimension.

### arm64 build constraint
All Docker images must build and run on `linux/arm64` (OCI Ampere A1). Flag any dependency that is x86-only or lacks Arm wheels as soon as it is noticed. Assume a small instance; keep memory modest; no GPU.

## Conventions

- **Python 3.12 everywhere** (local via uv, Docker, CI). Do not split versions.
- **Database enforces the guarantees** (unique idempotency index, conditional-UPDATE single-use tokens, append-only triggers, policy-validity exclusion constraint, `bigint` cents, decisions before actions). If a guarantee seems impractical to put in the database, say so rather than moving it silently into Python.
- Policy version selection and window measurement use distinct types `PurchaseDate` and `WindowAnchorDate` (`app/domain/types.py`). Never pass plain `date`.
- Use `clock_timestamp()`, not `now()`, for expiry checks inside long transactions.
- **Prompt Guard is detection only.** It must never be the control that blocks an action, and a negative result never relaxes a check. Auth, deterministic rules, and the single-use confirmation token are the only defenses.
- **Paid-amount invariant:** total refunds per order can never exceed the amount actually paid, enforced in code and at the database, independent of policy text.
- Policy version comes from the purchase date; the refund window is measured from delivery or presumed-lost date. Never conflate the two anchors.
- **Inject the clock.** No module under `app/` (or `tests/`) may call `date.today()`, `datetime.now()` or `datetime.utcnow()`; business date logic takes a `Clock` (`app/clock.py`). Tests and evaluation pin `FixedClock` to `SIMULATED_TODAY` (2026-04-15). Token expiry deliberately stays on the database's `clock_timestamp()`; do not unify the two (SPEC 2.5). A test enforces this.
- **LLM call budget: at most 2 calls per case** (extraction, explanation). A third needs PM approval.
- **Fail closed on explanation mismatch:** the customer sees the rules engine's Decision, never the model's numbers; disagreement escalates to handoff.
- FakeAdapter fixtures are keyed by scenario ID + step index; the prompt hash is a staleness warning only.
- Record arm64 availability and evidence in `docs/SPEC.md` section 9 when adding any heavy dependency. No arm64 CI job; real verification is a native build on OCI at M4.
- **Stage named paths, never `git add -A`** (a private key was once swept into a commit that way; see `docs/FAILURE_ANALYSIS.md` F-004). The pre-commit hook is the second layer behind `.gitignore`.
- Log real defects in `docs/FAILURE_ANALYSIS.md` with the test that exposed them.
- Small commits; tests alongside the code they cover.
- Never commit secrets. `.env` is git-ignored; provide `.env.example` with placeholder values only.
- **Ask before adding a dependency or expanding scope.**
- Typed schemas at every API boundary; business rules are pure functions with unit tests.
- Commit messages and pull request descriptions contain **no attribution trailers of any kind**. Never add `Co-Authored-By`, `Claude-Session`, or "Generated with Claude Code" lines. Subject and body only.
- **Never run `git push`.** Commits only. The project owner pushes manually after reviewing the diff. This is permanent; an earlier one-off instruction to push has been withdrawn.
- A pre-commit hook (`scripts/git-hooks/pre-commit`, installed at `.git/hooks/pre-commit`) refuses staged `*.key`, `*.pem`, `id_rsa*`, `id_ed25519*`, `.env` and `.env.*` (except `*.example`/`*.sample`), and any file whose first line contains `PRIVATE KEY`. Hooks live outside tracked content: in a new clone run `cp scripts/git-hooks/pre-commit .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit`. Never bypass it with `--no-verify`.
- Project code must not import `httpx2` (a transitive dependency of `huggingface-hub`); a test enforces this. Our HTTP client is `httpx`.
