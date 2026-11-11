# SPEC: Support Resolution Agent (SIMULATED DATA)

Status: draft v0.1, for project-manager review. Everything here is a proposal unless stated in `CLAUDE.md`. All customers, orders, and policies are synthetic.

## 1. Workflows

Common preamble for every workflow (steps 0-2 are never skipped):

0. **Authenticate.** The session cookie resolves to a `customer_id` server-side. Every tool and query receives that ID from the session, never from model output or message text.
1. **Classify intent** (model, structured output, validated against an enum): `order_status | cancel_subscription | refund_request | policy_question | other`. `other` or low confidence leads to a clarifying question or handoff.
2. **Retrieve policy** relevant to the intent, filtered by effective date (see 2.x below), and cite chunk IDs plus policy version.

### 1.1 Order-status lookup (read-only)
1. Preamble (auth, intent, policy retrieval for shipping policy).
2. Resolve which order: customer names an order or, if absent, list the customer's recent orders. Matching is done by code against the customer's own orders only; an order ID belonging to another customer returns "not found" (same response as a nonexistent ID, to avoid leaking existence).
3. Fetch live order, shipment, and subscription records via the internal API.
4. Code computes a status summary (state, carrier events, expected vs actual delivery, whether the order is past the "considered lost" threshold in the applicable shipping policy).
5. Model writes the reply from the structured summary and cited policy text. It may not add facts absent from the summary.
6. Return reply with citations and data timestamp ("as of ...").

### 1.2 Subscription cancellation (write)
1. Preamble (cancellation policy chosen by the subscription's start date / plan version).
2. Fetch live subscription (+ next billing date, commitment term, pending orders).
3. **Rule engine** returns a `Decision`: `allowed | allowed_with_conditions | denied | needs_human`, with reason codes, effective date (immediate vs end of period), and any fee/prorated amounts. Pure function, no model involvement.
4. Create a **Proposal** (immutable row): exact action, parameters, computed amounts, policy version, expiry, and a single-use confirmation token bound to the session and customer.
5. Show the proposal in the confirmation panel. The model's explanation is rendered alongside but the panel is built from the Proposal data, not model text.
6. Customer confirms (UI button posts the confirmation token; typed "yes" in chat is NOT sufficient, to resist injection).
7. **Execute** in one DB transaction: verify Proposal unexpired/unused, re-fetch live state and re-run rules (state may have changed; if the decision differs, abort and re-propose), insert `actions` row with unique idempotency key, apply change, write audit record.
8. Return a receipt (action ID, what changed, effective date, policy cited). On timeout or unknown outcome, state is `pending` and the receipt says so; a reconciliation read resolves it (see 5.4).

### 1.3 Refund request (write)
1. Preamble (refund policy chosen by order purchase date; shipping policy for the lost/late determination).
2. Fetch live order, line items, payments, shipments, prior refunds.
3. Rule engine decides eligibility per line item and computes the maximum refundable amount in integer minor units (cents): window from purchase/delivery date, reason class (not delivered, damaged, change of mind), prior refunds, shipping fee treatment, amount caps, and an auto-approve ceiling above which the result is `needs_human`.
4. Proposal, confirmation, and execution follow 1.2 steps 4-8. The refund row's idempotency key is derived from (customer_id, order_id, line_item_ids, reason, proposal_id), and a DB unique constraint on (order_id, line_item_id) for non-voided refunds prevents double refunds even if keys are mishandled.
5. A customer who asks for more than the computed amount gets the computed amount proposed (or a handoff), never the requested amount.

### 1.4 Combined requests
The example request ("replacement hasn't arrived, cancel and refund the missing shipment") is decomposed by code into ordered sub-actions (refund, then cancel) from the intent list. Each sub-action gets its own Decision; the customer sees one combined confirmation panel with independently listed items. Partial permission is allowed (refund yes, cancel denied with reason). Execution is per-action with its own idempotency key, so partial failure leaves a truthful state.

### 1.5 Handoff
Triggered by: rule engine `needs_human`, denied-but-disputed, repeated failures, tool-call cap reached, quota exhaustion, injection flagged, or customer request. A `handoffs` row stores the conversation, retrieved policy chunk IDs, API records fetched (snapshots), decisions and reason codes, actions attempted and outcomes, and the reason for handoff.

## 2. Data model (PostgreSQL)

**The authoritative schema is the Alembic migrations** (`backend/alembic/versions/0001_*` to `0004_*`); this section summarizes the design and what the database itself enforces. Money is `bigint` cents, timestamps are `timestamptz`, IDs are UUIDs. All data is synthetic.

Tables in migrations 0001/0002: `customers`, `auth_sessions`, `products`, `subscriptions`, `orders`, `order_items`, `shipments`, `shipment_events`, `policy_documents`, `policy_chunks`, `conversations`, `messages`, `decisions`, `decision_policies`, `confirmation_tokens`, `actions`, `refunds`, `refund_lines` (migration 0003), `audit_log`, plus the views `v_refund_drift` and `v_refund_line_drift`. Planned for later migrations: `handoffs`, `llm_cache`, `llm_calls`, `tool_calls`, `retrieval_logs`, `eval_runs`, `eval_case_results`.

Changes from the first draft: `proposals` is replaced by first-class `decisions` plus `confirmation_tokens`; `refunds` are line-level (PM override, migration 0003): a refund header has one or more `refund_lines`, and the paid-amount invariant is enforced at both levels; `payments` is dropped from the core schema because the simulated payments service (section 12.1) owns its own store and `orders.paid_cents` records what was actually paid.

### 2.1 What the database enforces
| Guarantee | Mechanism |
|---|---|
| Idempotency | `CREATE UNIQUE INDEX actions_idempotency_key_uq ON actions (idempotency_key)`; also `actions_decision_uq UNIQUE (decision_id)` so a decision executes at most once. The app catches the unique violation (SQLSTATE 23505, constraint name) and returns the original receipt. No pre-flight SELECT. |
| Single-use confirmation tokens | Conditional `UPDATE ... WHERE token_hash=$1 AND customer_id=$2 AND decision_id=$3 AND used_at IS NULL AND expires_at > clock_timestamp() RETURNING ...` (`app/db/tokens.py`); a guard trigger also forbids un-using, retargeting, deleting, or using an expired token. Only the SHA-256 of the token is stored. Composite FK binds the token to the customer and the specific decision. |
| Append-only | `forbid_mutation()` triggers (UPDATE, DELETE, TRUNCATE) on `audit_log`, `decisions`, `decision_policies`, `refunds` |
| No overlapping policy versions | `EXCLUDE USING gist (policy_type WITH =, validity WITH &&)` on `policy_documents`. **`btree_gist` is installed solely for this constraint**: it lets a gist index combine the equality on `policy_type` with the range-overlap operator. |
| Money | `bigint` everywhere, `CHECK >= 0`; `paid = list - discount + shipping` is a CHECK; a test asserts every `*_cents` column is `bigint` and no float/numeric column exists |
| Decision before action | `actions.decision_id NOT NULL` + composite FK; an `actions` trigger requires the decision to be `allowed`/`allowed_with_conditions` and the action type and amount to equal the decision's. Decisions are immutable and store `input_state`, `reason_codes`, `rule_set_version`, and (via `decision_policies`) the policy versions applied. |
| Paid-amount invariant | `CHECK (refunded_total_cents <= paid_cents)` on `orders` (`orders_refund_within_paid`); the `refunds` INSERT trigger is the only maintainer of the running total, so a refund row cannot exist without the check running. Not a policy rule. |
| Per-line cumulative guard | `order_items.refunded_cents` with `CHECK (refunded_cents <= paid_cents)` (`order_items_refunded_within_paid`), maintained only by the `refund_lines` INSERT trigger. `UNIQUE (refund_id, order_item_id)`; composite FKs force a line's refund and item to share one order and one customer. A deferred constraint trigger requires every refund header to equal the sum of its lines (and have at least one) at commit. Rules decide each line independently and the refund is the sum; no proportional splitting of order-level discounts beyond what `order_items.paid_cents` already encodes. |
| Ownership | see 2.2 |

Single action status flow is `pending` to `succeeded|failed` only; a final action cannot change.

### 2.2 Ownership (customer_id on the row)
- Every customer-scoped table carries `customer_id NOT NULL`. Child tables reference the parent by a **composite foreign key `(parent_id, customer_id)`**, so a child row cannot name a different customer than its parent (tested: `order_items_owner_fk`, `actions_decision_owner_fk`).
- **Where the filter is applied:** the application layer, in the repository functions, which take a `CustomerContext` built from the authenticated session and add `WHERE customer_id = :ctx_customer` to every customer-scoped query (never from model output). Not-found and not-yours return the same 404.
- **How a missing filter is caught:** (1) a schema test fails if any customer-scoped table lacks `customer_id NOT NULL`; (2) cross-customer integration tests (adversarial family 1) run against every repository function and assert zero rows/404 for another customer's IDs; (3) writes cannot cross customers regardless, thanks to the composite FKs. **Not enforced by the database today:** a missing `WHERE` on a *read* would not be stopped by the database. Row-level security with a non-superuser app role would close that gap (proposed for M2 hardening; see the task report).

### 2.3 Drift assertion
`orders.refunded_total_cents` and `order_items.refunded_cents` are denormalized. `v_refund_drift` recomputes `SUM(refunds.amount_cents)` per order; `v_refund_line_drift` checks each item's total against its lines and each order's total against the sum of all its lines. `assert_no_refund_drift()` (`app/db/invariants.py`) raises if either level disagrees. It runs in CI against seeded data and again after each evaluation suite run.

### 2.4 Concurrency (adversarial family 5)
The execute path takes `SELECT ... FROM orders WHERE id=$1 FOR UPDATE` before computing and writing, re-runs the rules on the locked row, and inserts the refund header and its lines in the same transaction. `tests/db/test_concurrent_refunds.py` runs two real connections on two threads against Postgres, both attempting the same full refund: with the lock exactly one refunds and the other declines cleanly; with the lock deliberately skipped, the database still rejects the second via `orders_refund_within_paid`.

### 2.5 Time: two clocks, deliberately not unified
- **Database wall clock (`clock_timestamp()`), for confirmation-token expiry.** Expiry operates on a timescale of minutes within a single request. `now()` is frozen at transaction start, so a long execution transaction would otherwise never observe a token expiring (found by a failing test, FAILURE_ANALYSIS F-001). This stays on the real clock. Pinning it would make an expired token look valid.
- **Injected `Clock` (`app/clock.py`), for all business date logic.** Policy windows are measured in days (30 and 45 day refund windows, 60 day proration, 14 day cooling-off, 10 business day presumed-lost). `Clock` is a protocol with `today()` and `now()`; `SystemClock` is production, `FixedClock` is for tests and evaluation. `SIMULATED_TODAY = 2026-04-15` is the one named constant for the simulated current date, after the 2026-03-01 cutover.
  - **Why:** seed data uses absolute dates. If the system read the real clock, every window would silently expire as real time passed and the test suite would rot. If seed dates were computed relative to the real clock instead, the evaluation suite would stop being reproducible, which would break M3. So tests and the evaluation runner pin the clock to `SIMULATED_TODAY`, and a test that depends on the real date is a bug.
  - **Enforcement:** `backend/tests/unit/test_no_direct_clock.py` fails if any module under `app/` (other than `app/clock.py` itself) or `tests/` calls or references `date.today`, `datetime.now` or `datetime.utcnow`, including through aliases and `default_factory=datetime.now`. It is static, so it does not see `getattr` tricks, `datetime.fromtimestamp(time.time())` or SQL `CURRENT_DATE`.
  - **Do not unify the two clocks.** A future reader tempted to route token expiry through the injected clock, or the business rules through the database clock, would break one of the two guarantees above.
  - **Consequence of the pinned date:** because the earliest possible v2 delivery is the cutover day and cutover plus 45 days is after 2026-04-15, no seeded v2 order can be outside its 45-day window. Window *expiry* under v2 is exercised by advancing the clock (`FixedClock.plus_days`) in rule tests, not by seed data.

### 2.6 Policy chunk identity (migration 0004)
Clause-level chunks carry a stable, human-meaningful identity and the date filter needs no join.
| Column / object | Purpose and enforcement |
|---|---|
| `policy_chunks.chunk_key` (`text NOT NULL`, `UNIQUE`) | Citation identifier such as `refund-v2-clause-3`. `policy_chunks_key_scheme` is a CHECK that it equals `policy_type \|\| '-v' \|\| version \|\| '-clause-' \|\| clause_number`, so the scheme cannot drift. Checked directly against the dev database on 2026-11-11: `chunk-7` and a well-formed key with the wrong clause number are both rejected with 23514. |
| `clause_number` (`integer NOT NULL`, `CHECK > 0`) | Position within the document, `UNIQUE (document_id, clause_number)`. |
| `policy_type`, `version`, `validity` (denormalized, `NOT NULL`) | Copied from the document so retrieval filters `validity @> purchase_date` on the chunk table alone. A composite FK `policy_chunks_document_identity_fk (document_id, policy_type, version, validity)` to `policy_documents_identity_uq` guarantees the copies always equal the document's values. |
| `policy_chunks_type_validity_idx` (gist on `policy_type, validity`) | Supports the date filter. |
| `policy_documents_guard_trg` (BEFORE UPDATE) | Rejects changes to `policy_type`, `version`, `validity`, `content_hash`, `title` (SQLSTATE 23001). A changed policy needs a new version. |
| `policy_chunks_guard_trg` (BEFORE UPDATE) | Rejects changes to `chunk_key`, `text`, `clause_number`, `document_id`, `policy_type`, `version`, `validity` (23001), so a citation keeps its meaning. Embedding and Prompt Guard verdict columns stay updatable. |

Ingestion (`app/retrieval/ingest.py`) is idempotent: a second run inserted 0 documents and 0 chunks and left every chunk key, row id, text hash and embedding hash unchanged (compared on the dev database, 2026-11-11). Seeded corpus: 5 documents, 19 chunks (cancellation v1 3, v2 2; refund v1 6, v2 5; shipping v1 3). Like the other guard triggers, these are bypassable by the table owner (2.7).

### 2.7 Limits of database enforcement
The table owner (and superusers) can drop triggers or disable them. The app should connect as a non-owner role in deployment; local development uses the compose superuser. Until that role exists, the append-only and invariant guarantees hold against application bugs, not against someone with owner credentials.

## 3. State machine

One `conversation` has one explicit state, transitions are enforced by a single function with an allow-list table, and every transition is audit-logged. A global counter caps tool calls (default 8 per turn, 20 per conversation, configurable); exceeding it transitions to `HANDED_OFF`.

States:
`NEW` -> `AUTHENTICATED` -> `INTENT_CLASSIFIED` -> `POLICY_RETRIEVED` -> `DATA_FETCHED` -> `DECIDED`

From `DECIDED`:
- informational outcome -> `ANSWERED` (terminal for the turn)
- actions permitted -> `AWAITING_CONFIRMATION`
- nothing permitted -> `DENIED_EXPLAINED` (terminal for the turn)
- needs human -> `HANDED_OFF`

From `AWAITING_CONFIRMATION`:
- confirm -> `EXECUTING`
- decline / expiry -> `CANCELLED`
- customer changes request -> back to `INTENT_CLASSIFIED`

From `EXECUTING`:
- all succeeded -> `COMPLETED` (receipt)
- timeout / unknown outcome -> `PENDING` (then reconciliation -> `COMPLETED` or `FAILED`)
- definite failure -> `FAILED` -> `HANDED_OFF` where appropriate
- state drift detected on re-check -> `AWAITING_CONFIRMATION` with a new proposal

Any state -> `HANDED_OFF` on: tool cap, quota exhausted past retry policy, injection flag requiring review, unrecoverable error.
Unauthenticated -> no transition past `NEW`.

The model is invoked only in `INTENT_CLASSIFIED` (classification/extraction) and `ANSWERED/DENIED_EXPLAINED/COMPLETED/...` (explanation). It never triggers a transition into `EXECUTING`.

## 4. Model provider layer

### 4.0 Free-tier model list (captured 2026-11-10, may change)

Captured from a live Groq key on 2026-11-10; the list **will change** and the adapter layer is what absorbs that (model IDs live only in env and adapter defaults):
- `openai/gpt-oss-120b`: primary chat model
- `openai/gpt-oss-20b`: fallback
- `qwen/qwen3.8-27b`: second fallback
- `meta-llama/llama-prompt-guard-2-86m` and `-22m`: prompt-injection classifiers
- No Llama 3.3 70B and no embedding model on the free tier.

gpt-oss models are reasoning models and may be less deterministic at temperature 0 than a standard instruct model. The smoke test therefore measures this directly: same prompt, 20 runs, temperature 0, report how many outputs are byte-identical. If determinism is poor, the cache still works as a replay store keyed by prompt, but reports must state that cached results are a **snapshot of one sampling, not a reproducible computation**.

Smoke-test target is gpt-oss-120b, running the checklist from the first report plus the determinism item.

### 4.0.1 Prompt Guard (detection only)

Llama Prompt Guard 2 is defense in depth and a **measurement layer**. It is never the control that prevents an unauthorized action. Server-side authorization, deterministic rules, and the single-use confirmation token remain the only things between hostile input and a write.
- It runs over retrieved policy chunks and customer-supplied text before they reach the main model.
- A positive detection annotates the trace and the handoff evidence and may raise a case to human review. It must not be the sole reason an action is blocked. A negative detection never relaxes any check.
- It is an extra network call outside the 2-call budget. It has its own budget, and its cost and latency are reported separately. If free-tier quota becomes the binding constraint, it is the first thing to drop.
- M3 reports its detection rate on adversarial families 2 and 3 as a separate metric, and separately demonstrates that the system holds on those same cases **with Prompt Guard disabled**. The disabled-run result is the one that matters.
- **Placement by trust, not by time:** classify text by whether it is attacker-influenced and whether it changes. (1) Policy documents, static and authored by us: scan **once at ingestion** and store the verdict with the chunk (`policy_chunks.injection_verdict`); never per request. (2) The customer message in the current turn: scan **per request**. (3) Any customer-writable field that later gets retrieved (order notes, profile text): scan **at write time AND keep treating it as untrusted at read time**; a stored clean verdict is never a reason to trust it later. Batch where the API allows.
- It sits behind the same provider interface and adapter layer (a `classify_injection` method), with a Fake implementation for CI.

### 4.1 Interface (`app/llm/base.py`)
```
class LLMProvider(Protocol):
    def complete(self, req: LLMRequest) -> LLMResponse
    def complete_structured(self, req: LLMRequest, schema: type[BaseModel]) -> StructuredResponse
```
`LLMRequest`: messages, temperature, max_tokens, optional tool schema and `tool_schema_version`. `LLMResponse`: text, parsed tool calls, usage (tokens in/out), provider-reported finish reason, latency. Errors are typed: `RateLimited`, `QuotaExhausted`, `ProviderTimeout`, `ProviderUnavailable`, `InvalidStructuredOutput`.

Design choice: **the structured-output contract is implemented in our code** (JSON schema in prompt, parse, validate with Pydantic, bounded repair retry), using native JSON mode / tool calling only as an optimization when the adapter reports support. This keeps behavior comparable across providers and with the Fake adapter.

### 4.2 Adapters (`app/llm/adapters/`)
- `GroqAdapter`: OpenAI-compatible HTTP; `base_url` and key from env. Prefer plain `httpx` to the OpenAI-compatible endpoint over a vendor SDK (fewer dependencies; ask before adding the `openai` package).
- `GeminiAdapter`: secondary, different request/response shape, translated to the same interface.
- `FakeAdapter`: reads fixtures from `tests/fixtures/llm/`, **keyed by scenario ID + step index** (PM decision 2026-11-10). The prompt hash is stored in the fixture as a staleness check only: on mismatch the adapter logs a loud warning naming the scenario and step and still returns the fixture (not a silent miss, not a hard failure). A missing scenario/step raises `FixtureMissing` (never invents output). A `RECORD=1` mode, run manually with a real provider, writes new fixtures. Staleness warnings are counted and surfaced in the test summary.
- Selection: `LLM_PROVIDER=groq|gemini|fake`, `LLM_MODEL=<id>`; model names appear only in `.env` and adapter defaults. A test greps the tree for vendor imports and model-name strings outside the adapter package and `.env.example`.

### 4.3 Rate limiting and retry (`app/llm/ratelimit.py`)
- Token buckets per provider for requests/min and tokens/min (estimated from prompt size before the call, corrected from usage after). Limits come from config because free-tier values change.
- A daily request counter persisted in Postgres (survives restarts); at the configured cap, calls fail fast with `QuotaExhausted`.
- On HTTP 429 or 5xx: exponential backoff with full jitter, honoring `Retry-After`, capped at N attempts (default 4) and a total wall-time budget. Beyond that, raise `RateLimited`. Daily-quota 429s are distinguished from per-minute ones and are not retried.
- Workflow mapping: any of these errors during intent classification gives a "service busy, try again" with state `FAILED`/`PENDING` and handoff offer; during explanation, the deterministic decision is still shown using a templated (non-model) message. No path produces a success claim.

### 4.4 Cache (`app/llm/cache.py`)
Key = `sha256(provider, model, canonical full prompt, temperature, tool_schema_version, prompt_template_version)`. Prompt template version is in the key so prompt drift produces a miss, not a stale hit. Stored in `llm_cache`. Modes: `read_write` (default), `replay_only` (miss raises `CacheMiss`, used by eval replays so reruns cost zero calls and are identical), `off`. Each lookup records hit/miss in `llm_calls` and the trace; eval reports include cache-hit rate. Responses with errors or invalid structure are not cached.

### 4.5 Request path
```
route -> workflow state machine -> LLMClient
LLMClient: cache lookup -> [hit: return] -> rate limiter -> adapter -> retry/backoff
           -> validate structured output -> cache write -> usage/latency logging -> return
```
Order matters: cache sits **outside** the limiter, so hits consume no quota. The `FakeAdapter` sits at the adapter position, so the cache, limiter, and validation logic are all exercised in CI without network.

### 4.6 Embeddings
`app/retrieval/embedder.py` wraps a local CPU embedder for bge-small-en-v1.5 (384-dim) with pinned name and revision hash (HF commit `5c38ec7c405ec4b44b94cc5a9bb96e735b38267a` as seen 2026-11-10; re-verify when pinning), loaded once per process. Runtime is **ONNX Runtime + tokenizers + numpy** (PM-approved 2026-11-10), with CLS pooling and L2 normalisation implemented by hand; no torch in project dependencies. The pinned revision hash was verified against the Hugging Face API on 2026-11-10. int8 quantization is deferred. Gate: the parity test in section 14 must pass before retrieval work builds on this embedder. Not part of the LLM provider interface and not a vendor LLM API. Documents are re-embedded only when `content_hash` or model id changes.

## 5. Cross-cutting design notes

1. **Authorization:** FastAPI dependency `current_customer` is the only source of customer ID. Tool functions take a `CustomerContext` created from it. Model tool-call arguments are validated against schemas that have no customer-ID field.
2. **Injection resistance:** policy chunks and customer text are placed in delimited data blocks, labeled untrusted; model outputs can only select from enumerated intents/fields. Backend ignores any "permission" inferred from text. Messages are scanned for obvious injection patterns for logging/flagging only (detection is not the defense; the architecture is).
3. **Idempotency:** client-supplied `Idempotency-Key` header on the execute endpoint plus the server-derived key in `actions`; repeat with same key returns the original result; same key with different payload returns 409. Unique constraints are the backstop.
4. **Timeout handling:** the (simulated) payments/fulfillment API call is made with an idempotency key; on timeout the action stays `pending`, and a reconciliation step queries by key. Never reported as success until confirmed.
5. **Policy applicability:** two different date anchors, never conflated (full rules in section 16.1). The policy **version** is selected by the order's **purchase date** (`PurchaseDate`; subscription start for cancellation). The refund **window** that version specifies is then measured from the **delivery date, or the presumed-lost date** when there was no delivery (`WindowAnchorDate`). Retrieval filters `policy_documents` with `validity @> purchase_date`. Rules in code carry the policy version they implement, and a test asserts code version matches the stored policy version. The two anchors are distinct `NewType`s (`app/domain/types.py`), so swapping them is a mypy error.
6. **Observability:** one `trace_id` per conversation; spans for retrieval, each LLM call, each tool call. Cost shown as **projected** using `PRICE_PER_1K_IN/OUT` config.

## 6. Repository layout (proposed)

```
.
├── CLAUDE.md
├── README.md
├── docs/                    SPEC.md, policies-design.md, eval notes, failure analysis
├── policies/                versioned policy sources (markdown + front-matter)
├── backend/
│   ├── pyproject.toml
│   ├── Dockerfile
│   ├── alembic/             migrations
│   ├── app/
│   │   ├── main.py
│   │   ├── config.py
│   │   ├── clock.py         Clock protocol, SystemClock, FixedClock, SIMULATED_TODAY
│   │   ├── seed/            deterministic synthetic data (definitions, ids, idempotent loader)
│   │   ├── api/             routers: auth, chat, orders, subscriptions, proposals, handoffs
│   │   ├── auth/            sessions, current_customer dependency
│   │   ├── domain/          models, rules/ (refund, cancel, shipping), money
│   │   ├── workflow/        state machine, orchestrator, tool registry, caps
│   │   ├── llm/             base.py, client.py, ratelimit.py, cache.py, adapters/
│   │   ├── retrieval/       chunking, embedder, fts, vector, fusion, citations
│   │   ├── observability/   tracing, cost
│   │   └── db/              session, repositories
│   └── tests/               unit/, integration/, fixtures/llm/
├── frontend/                React app (chat, confirmation panel, account, handoff queue)
├── eval/                    dev set, held-out set (locked), adversarial, runner, reports
├── docker-compose.yml
├── .env.example
└── .github/workflows/ci.yml
```

## 7. M1 task breakdown

M1 goal: one order-status conversation works end to end with citations. Each task is a small commit with tests.

1. **Repo scaffold** (needs approval of dependency list first): backend `pyproject`, FastAPI app with `/health`, config via env, lint/type/test tooling.
2. **docker-compose + Postgres with pgvector**, multi-arch image verified on arm64 (buildx/QEMU check in CI later). `.env.example`.
3. **Migrations** for customers, auth_sessions, products, orders, order_items, shipments, shipment_events, subscriptions, payments, policy tables, audit_log, conversations, messages.
4. **Policy corpus:** write shipping, refund, cancellation policies, each with at least two versions and effective dates, plus a labeled-simulated banner. Ingest script (chunk, tsvector, embed, store model id).
5. **Synthetic seed generator:** deterministic (fixed seed), ~20 customers, orders across dates straddling policy changes, shipments with late/lost/delivered cases, subscriptions.
6. **Minimal auth:** seeded customers log in, server-side session, `current_customer` dependency. Tests: no session means 401; cross-customer order access returns 404.
7. **Order and shipment read APIs** scoped to the session customer, typed schemas, tests.
8. **Provider layer core:** interface, typed errors, `FakeAdapter` with fixtures, `LLMClient` skeleton with cache (`llm_cache`) and trace logging. Tests with zero network.
9. **GroqAdapter + rate limiter + retry** with unit tests using a fake HTTP transport; manual smoke test script (excluded from CI) per the verification list in the report.
10. **Retrieval baseline:** FTS first, then pgvector, then reciprocal rank fusion; date-aware policy filter; retrieval logging. Tests on seeded corpus incl. wrong-version-is-never-returned.
11. **Order-status workflow:** minimal state machine (NEW to ANSWERED/HANDED_OFF), intent classification via structured output, order resolution in code, summary builder, model explanation, citation formatting.
12. **Chat endpoint** `/chat` returning reply, citations, data-as-of timestamp, trace ID.
13. **Minimal React chat UI** (login, chat, citations, SIMULATED banner).
14. **CI:** GitHub Actions running lint, type check, tests with FakeAdapter and a Postgres service, no secrets; vendor-import guard test.
15. **M1 demo script + README section** documenting the end-to-end conversation.

M1 exit check: scripted conversation "Where is my order #X?" passes in CI using fixtures, and one manual run against Groq succeeds and is cached.

## 8. Decisions log (PM-approved 2026-11-10)

- Embedding model bge-small-en-v1.5, revision pinned; Groq via `httpx` (no `openai` SDK); `uv`, `ruff`, `mypy`; Alembic + SQLAlchemy 2.0 + psycopg 3; Vite + React + TypeScript with no UI library in M1; minimal session login in M1, hardening in M2.
- Two versions per policy; change date (2026-03-01) inside the seeded order range.
- Locked `eval/heldout/` with a hash manifest and a CI check that fails if locked files change.
- **No arm64 CI job.** Dockerfiles stay architecture-neutral; arm64 availability of each heavy dependency is recorded in section 9 with evidence; real arm64 verification is a native build on OCI at M4.
- Assumptions 1-15 from the first report accepted. Load-bearing: confirmation is a UI button posting a single-use token (never typed "yes"); live state is re-fetched and rules re-run at execution time, re-proposing if the decision changed (TOCTOU handling is a release requirement); combined requests are split into separate actions with separate Decision records.
- **Deployment fallback:** attempt OCI A1; if capacity is unavailable, ship a recorded demo plus a documented docker compose quickstart and note the capacity issue in the README. Cap OCI capacity effort at two hours.

## 9. Dependency arm64 / weight evidence

Checked 2026-11-10 against PyPI JSON metadata (wheel filenames) and the Hugging Face API. "Has aarch64 wheel" means a `manylinux*_aarch64` wheel exists for CPython 3.11 (or abi3) at the listed version. This is metadata evidence only; real runtime verification happens at M4 on OCI.

| Dependency | Version checked | aarch64 evidence | Notes |
|---|---|---|---|
| pydantic-core | 2.50.4 | `manylinux2014_aarch64` cp311/cp312 wheel, 2.1 MB | via FastAPI/pydantic |
| psycopg-binary | 3.3.8 | `manylinux_2_28_aarch64` cp311/cp312 wheel, 6.9 MB | in use since task 3 |
| tokenizers | 0.23.3 | `cp310-abi3-manylinux_2_17_aarch64` wheel, 3.5 MB | **declares a hard dependency on `huggingface-hub`** (found when it was installed; earlier estimates counted only the 3.5 MB wheel) |
| huggingface-hub (via tokenizers) | 2.4.0 | pure Python; requires click, filelock, fsspec, hf-xet, httpx2, packaging, pyyaml, tqdm, typing-extensions | transitive. `httpx2` is therefore in the lockfile even though project code uses `httpx`; our code does not import it |
| hf-xet (via huggingface-hub) | 1.8.0 | `cp38-abi3-manylinux_2_28_aarch64` wheel, 4.1 MB | native; marker includes `aarch64` |
| onnxruntime | 1.31.1 | cp311/cp312 `manylinux_2_28_aarch64` wheel, 21.4 MB | |
| numpy | 2.5.4 | cp312 `manylinux_2_27_aarch64` wheel, 15.7 MB | Python is 3.12 everywhere (section 15), which resolves the earlier cp311 gap |
| pgvector/pgvector image | `0.8.7-pg17` (digest `sha256:7d0a4146...ccb8`) | `docker buildx imagetools inspect` and the Docker Hub tag API both list `linux/amd64` and `linux/arm64`; the same holds for the 0.7.4 to 0.8.7 pg17 tags | multi-arch confirmed by manifest. Run locally on amd64 only: extension loaded, `<->` distance and tsvector queries worked |
| sqlalchemy | 2.1.8 | `cp312 manylinux_2_28_aarch64` wheel, 4.7 MB (C extensions); pure-Python fallback exists | greenlet (async only) not installed |
| alembic / mako | 1.20.1 / 1.4.3 | pure Python | |
| markupsafe | 3.0.4 | `cp312 manylinux_2_28_aarch64` wheel | via mako |
| python:3.12-slim-bookworm | base image | manifest lists `linux/amd64`, `linux/arm64/v8`, arm/v7 and others | |
| ghcr.io/astral-sh/uv | 0.11.19 | manifest lists `linux/amd64` and `linux/arm64` | copied into the build stage only |
| torch | 2.15.0 | cp311 `manylinux_2_28_aarch64` wheel, **484 MB** (x86_64: 539 MB); metadata lists `nvidia-*` Linux deps | heavy; needs CPU-only index, and verify CUDA packages are not pulled |
| sentence-transformers | 6.2.0 | pure Python; requires torch, transformers, scikit-learn, scipy | pulls the heavy tree |
| fastembed | 0.9.1 | pure-Python wheel, 150 KB; requires onnxruntime, tokenizers, numpy, pillow, loguru, mmh3, py-rust-stemmers, requests, huggingface-hub, tqdm | no torch; mmh3, py-rust-stemmers, pillow not yet checked for aarch64 |
| bge-small-en-v1.5 ONNX | HF `onnx/model.onnx` | 133 MB fp32 in the official repo | architecture-independent file |

Embedding runtime decision: ONNX Runtime + tokenizers + numpy (plus tokenizers' transitive huggingface-hub tree, about a dozen small packages) is well under 100 MB of wheels plus the 133 MB model, versus several hundred MB to over a gigabyte for torch. The earlier "about 40 MB" figure ignored the transitive packages; see the tokenizers row. A hand-written WordPiece tokenizer would remove that tree and is **rejected**; see section 9.1. Approved by the PM; the embedder sits behind an interface local to `app/retrieval/embedder.py`.

The backend image built locally on amd64 at 317 MB before the embedder, and **230 MB compressed / 582 MB uncompressed with the embedder, model and policy corpus** (details in 9.1; re-measured 2026-11-11 after setting `UV_NO_CACHE=1`; it was 296 MB / 795 MB before). The Dockerfile contains nothing architecture-specific, but it has not been built on arm64.

| Image | Size (amd64, 2026-11-11) | arm64 evidence |
|---|---|---|
| `support-backend` (app + onnxruntime + tokenizers + numpy + bge-small ONNX model) | 230 MB compressed, 582 MB uncompressed | not built on arm64; every dependency row above has an aarch64 wheel, the base images list `linux/arm64`, and the model file is architecture-independent. Native verification is the OCI build at M4. |

### 9.1 Decision: keep `tokenizers`; do not hand-write a tokenizer (PM, 2026-11-10)

- **Rejected alternative:** a hand-written WordPiece tokenizer (roughly 60 lines) to drop `huggingface-hub` and its tree.
- **Why rejected (correctness, not effort):** an implementation that is 99 percent correct yields subtly wrong embeddings on exactly the unicode and adversarial inputs this project cares about (accent stripping, CJK splitting, control characters, zero-width joiners, homoglyphs). Tokenizer bugs are input-specific, so a 34-string parity fixture would not reliably catch them. It trades a measured, bounded dependency cost for an unmeasured correctness risk in retrieval.
- **What it would have saved:** the transitive tree (about a dozen small packages, tens of MB). Not a binding constraint: the backend image was 317 MB before the embedder, against an OCI instance with at least 6 GB of memory. The same reasoning keeps int8 quantization deferred.
- **Revisit only on a measured problem**, such as the OCI instance actually running short of disk.
- **`httpx2`:** a transitive dependency is acceptable. Project code must not import `httpx2` or `httpcore2`; `backend/tests/unit/test_import_boundaries.py` fails if any module under `app/` does, so the boundary is enforced by CI rather than remembered.
- **Image size with the embedder (measured 2026-11-11, linux/amd64 only):** the Dockerfile fetches and verifies the pinned model during the build (fails on a SHA-256 mismatch). The build succeeded, and inside the container the embedder loaded and returned a 384-dimension vector as the non-root user. Sizes, by method (they differ and are not interchangeable):
  - **Current (after `UV_NO_CACHE=1` and adding `policies/`): 230 MB compressed** (`docker image inspect .Size`) and **582 MB uncompressed** (`du -sx /` as root). Layers: base OS 85 MB, python 55 MB, uv binary 58 MB, dependencies 264 MB, model 134 MB, policies 41 kB. The earlier 317 MB pre-embedder baseline used a measurement method that was not recorded, so it is not strictly comparable to either figure.
  - **Before the fix (first measurement):** 296 MB compressed, 795 MB uncompressed. About 208 MB of the dependency layer was uv's download cache in `/root/.cache/uv`; it is gone (`/root/.cache` no longer exists in the image). The model (134 MB) is now about 23 percent of the uncompressed image.
  - `docker image ls` reports a larger figure under Docker Desktop's containerd store (1.11 GB for the first build); it does not match the layer sizes and is not used here.
  - **Policy corpus in the image:** `policies/` is passed as a named build context (`--build-context policies=./policies`; `docker-compose.yml` sets `additional_contexts`) and copied to `/policies`, where the loader's `<parents[3]>/policies` lookup finds it. A named context was chosen over widening the build context to the repo root so that the repo root, including untracked files such as private keys, is never sent to the daemon. Verified: `python -m app.retrieval.ingest` runs inside the container against the dev database and reports `chunks_unchanged=19`.
  - **Gap found, not fixed:** the image does not contain `alembic.ini` or `alembic/`, so migrations cannot run from the image. The M4 deployment needs a decision (copy them in, or run migrations from a separate one-shot image).

## 10. Response path and explanation correctness

### 10.1 Structured explanation output
The explanation call returns a validated object:
`{outcome, amount_cents, policy_ids_cited[], explanation_text}`.

### 10.2 Fail-closed display rule
The customer-facing outcome, amount, and cited policy IDs always come from the rules engine's **Decision record**, never from the model. The model's `outcome`, `amount_cents`, and `policy_ids_cited` exist only to be compared against the Decision. If any disagree, or the output fails validation, the turn **fails closed**: neither value is displayed, the faithfulness failure is logged, and the conversation moves to `HANDED_OFF` with a templated non-model message. `explanation_text` is shown only after the comparison passes, and is also scanned for any currency amount that differs from the Decision.

### 10.3 Metrics
1. **Faithfulness (deterministic):** per case, assert outcome, amount_cents and policy_ids equal the Decision. Every mismatch is counted and reported (rate with a binomial CI, plus examples).
2. **Citation support (sampled):** does the cited policy text support the stated basis? A fixed sample is audited manually against a written rubric (`eval/rubric.md`, written before M3); the remainder is model-judged. The report states the sample size and the human/model agreement rate (percent agreement and Cohen's kappa). Model-only grading is never reported as evidence.

## 11. LLM call budget and resumable eval runner

- **Hard cap: 2 LLM calls per case** (1: intent + entity extraction; 2: explanation). A third call requires PM approval with a reason. The orchestrator enforces the cap in code, and the eval report instruments and asserts per-case call counts, so a regression is visible (a CI test fails if any fixture case exceeds 2). Repair retries for invalid structured output count against the cap; if the cap blocks a repair, the turn fails closed.
- **Resumable runner, designed in from the start:** each completed case is checkpointed to Postgres (`eval_runs`, `eval_case_results`). `--resume` skips completed cases; `--replay-only` errors on a cache miss instead of calling live. Per case we record: provider, model ID, prompt template version, cache hit/miss, and cache snapshot identifier. An interrupted run never restarts from zero.

## 12. Adversarial and failure-injection suite

Minimum 40 cases, at least 4 per family. Every zero-failure claim is reported with a binomial confidence interval (Clopper-Pearson 95%; 0 failures in 40 gives an upper bound of about 8.8%, and the report must say so).

1. Cross-customer read and write via identifier manipulation
2. Prompt injection inside retrieved policy documents
3. Prompt injection inside the customer message or order notes
4. Replay of a confirmation token
5. Concurrent duplicate confirmed requests (a genuine race: real concurrent connections against Postgres)
6. Stale decision: live state changes between propose and execute
7. Amount manipulation: customer asserts a larger refund and cites invented policy
8. Policy version confusion: customer claims newer terms apply to an older order
9. Downstream timeout that later succeeds in the background
10. Quota exhaustion partway through a workflow

### 12.1 Simulated payments service
A separate in-repo module with its own process boundary and store that **honors idempotency keys itself** (same key returns the original result; same key with a different payload is rejected) and can be told to model: normal success, definite failure, a request that **times out from our side but succeeds downstream** (committed after the client gives up), and slow responses. Without the timeout-then-succeeds mode the exactly-once guarantee is untested. Reconciliation queries it by idempotency key.

## 13. Seeded policy content (SIMULATED BUSINESS)

Specified by the PM, not invented. Every policy document, seed row, UI screen and report is labeled as a simulated business. Money is integer cents. "Business days" means Monday to Friday with no holiday calendar (assumption).

**Shipping policy (single version)**
- Standard domestic delivery window: 7 business days from ship date.
- A shipment is presumed lost if undelivered 10 business days after ship date.
- A replacement shipment carries its own window measured from its own ship date.

**Refund policy v1** (effective 2025-06-01 to 2026-02-28)
- Refund window: 30 days from delivery date, or from the presumed-lost date if never delivered.
- Lost or undelivered shipments: full refund of the shipment value.
- Auto-approve ceiling: 7500 cents; above that, human review.
- Partially consumed subscription boxes: prorated by items shipped.
- Digital add-ons: non-refundable once accessed.
- Items flagged `final_sale`: non-refundable regardless of window.

**Refund policy v2** (effective 2026-03-01)
- Refund window extended to 45 days.
- Auto-approve ceiling raised to 12000 cents.
- A carrier "delivery exception" status permits a lost-shipment refund without waiting the full presumed-lost window.
- Orders placed with a promotional discount refund the amount actually paid, never list price.
- Digital add-on and `final_sale` rules unchanged.

**Cancellation policy v1** (effective 2025-06-01 to 2026-02-28)
- Cancellation permitted at any time, effective at the end of the current billing period.
- The current period is not refunded.
- Annual plans: prorated refund of unused whole months, less a 10 percent administrative fee, available within the first 60 days only.

**Cancellation policy v2** (effective 2026-03-01)
- As v1, but the administrative fee is removed.
- Adds a 14-day cooling-off period: cancellation within 14 days of the first charge refunds that charge in full.

### 13.1 Required seed cases (straddling 2026-03-01 so both versions are exercised)
At least one of each (eight cases):
1. a refund computing above the v1 ceiling (7500) but below the v2 ceiling (12000)
2. a promotional-discount order where list price and paid amount differ
3. an order containing an accessed digital add-on alongside refundable items
4. a `final_sale` item
5. a presumed-lost shipment with a replacement already sent
6. an annual subscription inside the 60-day proration window
7. a subscription inside the v2 cooling-off period
8. an annual subscription under v2 cancelled on day 10 after the first charge, where both cooling-off and proration could fire (a test asserts cooling-off wins and fails if the evaluation order is reversed)

Seed tests assert each case exists. The interpretation points raised earlier are resolved in section 16.

### 13.2 Implemented seed (`app/seed/`, task 5)

Three customers (Alex, Blair, Carol; all labeled SIMULATED), 8 products, 5 subscriptions, 14 orders (5 are first-charge orders of the subscriptions), 22 order items, 9 shipments, 23 shipment events. Every date is an absolute literal in `app/seed/definitions.py`; there is no randomness; row ids are `uuid5(namespace, kind:key)`, so a test names a case by key and needs no database lookup. `python -m app.seed` is idempotent (a second run inserts nothing, verified by comparing every column of all seven tables, event ids included, and again on a second freshly migrated database), fails if a stored row differs from its definition, and ends with `assert_no_refund_drift()` in the same transaction. Seeded statuses are treated as live state and are not compared on re-seed. No refunds, decisions or actions are seeded: those records must come from the rules engine, and inventing them would plant fabricated audit history.

Positions relative to `SIMULATED_TODAY` (2026-04-15). "Day N" is calendar days since the window anchor (delivery, or presumed-lost date) for orders and since the first charge for subscriptions. Version is chosen by purchase date (orders) or start date (subscriptions).

| # | Key | Where | Version | Position at 2026-04-15 |
|---|---|---|---|---|
| 1 | `refund_v1_above_ceiling_human_review` | SIM-1001 Alex | v1 (purchase 02-26) | delivered 03-18, day 28 of 30: inside, 2 days left. Refundable 9800 is above the 7500 v1 ceiling and below the 12000 v2 ceiling: human review. Anchor is after the cutover while the version is v1. |
| 2 | `promo_discount_paid_differs_from_list` | SIM-1002 Alex | v2 | delivered 03-27, day 19 of 45: inside. List 8000, discount 2000, shipping 500, paid 6500; refundable lines 6000. |
| 3 | `digital_addon_accessed_with_refundable_items` | SIM-1003 Alex | v2 | delivered 03-31, day 15 of 45: inside. Add-on (1500) accessed 03-30; refundable 8000 of 9500. |
| 4 | `final_sale_item_in_order` | SIM-1004 Alex | v2 | delivered 04-03, day 12 of 45: inside. Final-sale mug (2000) excluded; refundable 3000 of 5500. |
| 5 | `lost_shipment_with_replacement_sent` | SIM-2001 Blair | v2 | undelivered; presumed lost 03-25 (10 business days after ship 03-11), day 21 of 45: inside. Replacement `replacement_in_transit` (SIM-2002, paid 0) placed 04-03 after the loss, shipped 04-06, expected 04-15, presumed lost 04-20 (its own window): in transit. |
| 6 | `annual_v1_inside_proration_window` | Blair | v1 (start 02-20) | day 54: inside the 60-day proration window (6 days left); v1 has no cooling-off; the 10 percent fee applies. |
| 7 | `monthly_v2_inside_cooling_off` | Carol | v2 (start 04-08) | day 7: inside the 14-day cooling-off. Monthly, so proration cannot apply. |
| 8 | `annual_v2_day10_cooling_off_beats_proration` | Alex | v2 (start 04-05) | day 10: inside cooling-off (14) and inside proration (60). Both could fire; cooling-off must win (16.3). The seed test asserts the conflict is present; the rules-engine test that proves the evaluation order is a later task. |
| - | `v1_order_delivered_after_cutover_window_expired` | SIM-1005 Alex | v1 (purchase 02-25) | delivered 03-05, day 41: outside the 30-day v1 window, inside what a 45-day window would be. Fails if the version is chosen by delivery date. |
| - | `in_transit_on_schedule` | SIM-2003 Blair | v2 | shipped 04-13, expected 04-22, presumed lost 04-27. For order-status lookups. |
| - | `v2_delivery_exception_before_presumed_lost` | SIM-3001 Carol | v2 | carrier exception 04-09; presumed lost 04-16, one day away. v2 clause 3 permits a lost-shipment refund anyway. |
| - | `annual_v2_inside_proration_after_cooling_off` | Carol | v2 (start 03-10) | day 36: past cooling-off, inside proration, no fee. |
| - | `annual_v1_outside_proration_window` | Blair | v1 (start 2025-11-03) | day 163: outside proration; cancellation at period end, no refund. |

`backend/tests/unit/test_seed_definitions.py` asserts each of these properties from the definitions and the real policy text (ceilings, window lengths, cooling-off and proration days are parsed from `policies/`, not hardcoded), and `backend/tests/db/test_seed.py` checks what actually landed in PostgreSQL. Deliberately breaking cases 1, 4 and 8 in the definitions makes the corresponding test fail (checked once by hand on 2026-11-11).

## 14. Embedding parity gate

Blocking gate before retrieval is built on the ONNX embedder:
1. In a **throwaway environment outside the project dependencies**, install sentence-transformers once and generate reference embeddings for at least 30 fixed strings, including real policy chunks, adversarial strings, and unicode edge cases (accents, CJK, emoji, empty-ish whitespace, very long text).
2. Commit the reference vectors as a test fixture (`backend/tests/fixtures/embedding_parity.json`) recording the sentence-transformers version, the model name, and the model revision hash that produced them.
3. The committed parity test loads the fixture and asserts cosine similarity of at least 0.999 against our ONNX implementation for every string. It runs in CI with no torch and no network (the ONNX model file is fetched by a setup script outside CI or cached; if the file is absent the test fails loudly rather than skipping).

Pinned model: `BAAI/bge-small-en-v1.5`, revision `5c38ec7c405ec4b44b94cc5a9bb96e735b38267a`, verified via the Hugging Face API on 2026-11-10. Dimension 384.

**Status (2026-11-10): gate passed.** Reference vectors: 34 strings (9 policy chunks from section 13, 4 customer messages, 6 adversarial including a 700-repeat string far past 512 tokens, 9 unicode cases, 5 edge cases including empty and whitespace-only strings), generated with sentence-transformers 6.2.0, transformers 5.22.2, torch 2.15.0 on Python 3.12, recorded in the fixture's `meta`. Result: minimum cosine 0.9999999, median 1.0000, against a threshold of 0.999. Test: `backend/tests/unit/test_embedding_parity.py` (no torch, no network). It also checks unit length and that batching/padding does not change a vector. Generator: `backend/scripts/generate_embedding_parity_fixture.py` (run in a throwaway environment, not in project dependencies).

**Implementation details that must match:** CLS-token pooling, L2 normalisation, 512-token truncation, lowercase/accent handling from `tokenizer.json`, no query-instruction prefix.

**Integrity controls and their limits (stated plainly).** The primary integrity control is the **pinned Hugging Face revision** (a commit hash). The recorded SHA-256 of each file is a **secondary check**. For `model.onnx` the hash is Hugging Face's own LFS object id, recorded independently of our download. For `tokenizer.json` the hash was **computed from our first download (trust on first use)**, so it detects later corruption or substitution but **cannot detect a compromised first download**; it is not independent verification. We cannot fully close this gap without a separately published hash for that file. CI and the Docker build fail on any mismatch.

**Model files and CI.** `model.onnx` is 133 MB, over GitHub's 100 MB per-file limit, so it is not committed. `python -m app.retrieval.fetch_model` downloads the pinned files and verifies SHA-256 (the `model.onnx` hash is the Hugging Face LFS object id; `tokenizer.json`'s hash was computed on first download, i.e. trust on first use). The test never touches the network; it reads files placed on disk. CI runs the fetch as a separate setup step with `actions/cache` (the key is a hash of `app/retrieval/embedding_model.py`, which holds the pinned revision and file hashes), then runs the tests with no network. **A hash mismatch fails the job and stops it, never a warning** (`fetch_model` exits non-zero; the step aborts the job). If the files are absent the parity test FAILS loudly, never skips. "No network in the test" means the test makes no network call; CI itself may use the network for this one setup step (PM-confirmed).

**Rejected alternatives for the model file:** Git LFS (the free quota is small and it makes the repository heavier to clone, which matters for a portfolio repo people may clone); the int8-quantized model (changing the model to fit a CI constraint lets infrastructure drive a retrieval-quality decision, the wrong order).

**CI workflow:** `.github/workflows/ci.yml` (written 2026-11-10; it has not yet run on GitHub). It runs a secret scan of tracked files, `uv sync --frozen`, the model fetch, ruff, mypy and pytest against a pgvector service container, with no API keys.

## 15. Python version

Python 3.12 everywhere: `requires-python >=3.12`, `.python-version` is 3.12, ruff and mypy target 3.12, the Dockerfile uses `python:3.12-slim-bookworm`, and `uv` provisions 3.12 locally. No local/container version split.

## 16. Policy interpretations (PM-resolved 2026-11-10)

### 16.1 Version selection vs window measurement: two different anchors
- **Version selection uses the purchase date.** An order placed 2026-02-20 and lost on 2026-03-10 is governed by refund policy v1, because refund terms form part of the purchase contract.
- **Window measurement uses a different anchor.** Once the version is chosen, the refund window that version specifies is measured from the delivery date, or from the presumed-lost date if the shipment was never delivered.
- These must never be conflated. Required test: a case where the purchase date selects v1 (30-day window) while the delivery or presumed-lost date falls after 2026-03-01, and the outcome differs depending on which anchor is used for which purpose. It must fail if the anchors are swapped or merged. The rule function takes `purchase_date` and `window_anchor_date` as separately named parameters.
- **Rejected alternative:** "apply whichever version is more favorable to the customer." Considered and rejected for v1 scope because it roughly doubles the rule matrix for no additional engineering signal.

### 16.2 Discount first, then prorate; and the paid-amount invariant
- Always compute from the amount **actually paid**, never from list price, then apply proration to that. The reverse order can produce a refund above what the customer paid.
- **System invariant (not a policy rule):** the sum of all refunds ever issued against an order can never exceed the amount actually paid for that order. It is enforced in code as the final check before any refund write, inside the same transaction that takes a row lock on the order, backed by a database-level guard (a trigger or constraint on cumulative refunded amount) so that violation is impossible rather than unlikely. It is tested directly, including with concurrent requests, and is independent of policy text. It must never be described as a policy rule.
- Refund policy v2 additionally states "refund the amount actually paid" as customer-facing policy. v1 is silent and relies on the invariant. The two mechanisms stay separate: the policy text explains to the customer, the invariant protects the business regardless of what any policy says.

### 16.3 Cancellation refund evaluation order (first match wins)
Evaluate in this order and stop at the first rule that applies:
1. **v2 cooling-off:** within 14 days of the first charge, refund that charge in full.
2. **Proration:** within the first 60 days of an annual plan, refund unused whole months, less the 10 percent administrative fee under v1 only.
3. **Otherwise:** cancellation takes effect at the end of the current billing period, with no refund.

Cooling-off takes precedence where both could apply, as the more specific, more favorable, tightly time-bounded rule. Implementation is an ordered list of rule objects, iterated in order, not nested conditionals. Required test (seed case 8): annual plan under v2 cancelled on day 10; assert cooling-off wins; the test must fail if the evaluation order is reversed.

### 16.4 Business days
Monday to Friday, no holiday calendar. The rule lives in **one module** (`app/domain/rules/business_days.py`) with the definition in its docstring, and is listed in the README limitations section. A holiday calendar would be more correct and carries no signal; the undocumented simplification would be the defect.

## 17. Row-level security and database roles (M2 task 0, PM-approved 2026-11-10)

Converts ownership (requirement g) from convention to enforcement and addresses section 2.7. Done at the start of M2, before repositories multiply. Each of the four points below is a way RLS silently does nothing.

### 17.1 Roles: who owns what
| Role | Purpose | Attributes |
|---|---|---|
| bootstrap superuser (compose `POSTGRES_USER`, OCI admin) | creates databases and roles only; never used by the app or by tests | superuser |
| `support_owner` | owns all tables, runs Alembic migrations | `LOGIN`, not superuser, `NOBYPASSRLS` |
| `support_app` | the application's runtime role | `LOGIN`, `NOSUPERUSER`, `NOBYPASSRLS`, **not the owner** of any table; grants limited to the DML it needs (SELECT/INSERT/UPDATE per table as required; no DELETE, TRUNCATE, DDL, or trigger control; no TRIGGER privilege) |
Superusers and table owners bypass RLS, so the app role must be neither.

### 17.2 Policies
- `ENABLE ROW LEVEL SECURITY` and `FORCE ROW LEVEL SECURITY` on every customer-scoped table (force is defense in depth against a future ownership mistake). Policy: `customer_id = current_setting('app.current_customer_id', true)::uuid`. An unset setting yields NULL and therefore no rows (fails closed). `customers` is protected on `id`. Reference data (`products`, `policy_*`) is read-only to the app role and not customer-scoped.
- The current customer is set with `SET LOCAL` (`set_config('app.current_customer_id', :id, true)`), **never `SET`**. Session-scoped state leaks across requests through a connection pool, which would be a cross-customer leak introduced by the very feature meant to prevent one. The transaction wraps the whole request, so the setting is transaction-scoped.

### 17.3 Tests
- Every RLS test connects as `support_app`. A test run as the compose superuser proves nothing.
- One test deliberately connects as the superuser and asserts the policy is bypassed, so the suite documents the boundary instead of hiding it.
- A pool test checks that a connection reused by a second request carries no customer setting from the first.
- Missing-setting test: queries with no customer set return zero rows.

### 17.4 Deployment
Role creation lives in a bootstrap script (`db/roles.sql`), referenced by `docs/DEPLOYMENT.md` so it does not evaporate at the OCI step. Passwords come from the environment, never the repository.
