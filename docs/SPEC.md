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

Money is `integer` minor units plus `currency` char(3). Timestamps are `timestamptz`. IDs are UUIDs except human-facing order numbers. Every table that is synthetic carries no real PII.

| Table | Key columns |
|---|---|
| `customers` | id, email (fake), display_name, password_hash (demo auth), created_at |
| `auth_sessions` | id, customer_id, expires_at, created_at, revoked_at |
| `products` | id, sku, name, price_cents |
| `orders` | id, order_number, customer_id, placed_at, status, total_cents, shipping_cents, currency, subscription_id (nullable), is_replacement_for (nullable order id) |
| `order_items` | id, order_id, product_id, qty, unit_price_cents, refunded_cents |
| `shipments` | id, order_id, carrier, tracking_no, status, shipped_at, delivered_at, expected_at |
| `shipment_events` | id, shipment_id, occurred_at, code, detail |
| `subscriptions` | id, customer_id, plan, status, started_at, current_period_end, commitment_end, canceled_at, cancel_effective_at, price_cents |
| `payments` | id, order_id, amount_cents, status, provider_ref (fake) |
| `policy_documents` | id, policy_key (shipping/refund/cancellation), version, effective_from, effective_to, title, source_path, content_hash |
| `policy_chunks` | id, document_id, ordinal, text, tsv (tsvector), embedding vector(384), embedding_model (text, NOT NULL), embedding_model_revision |
| `conversations` | id, customer_id, state, created_at, tool_calls_used |
| `messages` | id, conversation_id, role, content, created_at, injection_flags |
| `proposals` | id, conversation_id, customer_id, action_type, params jsonb, decision jsonb, policy_version_refs jsonb, confirmation_token_hash, expires_at, consumed_at |
| `actions` | id, proposal_id, customer_id, action_type, idempotency_key (UNIQUE), status (pending/succeeded/failed), result jsonb, created_at, completed_at |
| `refunds` | id, action_id, order_id, order_item_id, amount_cents, status; partial UNIQUE (order_item_id) WHERE status <> 'voided' |
| `audit_log` | id, at, actor_type (customer/system/human), actor_id, event, entity_type, entity_id, before jsonb, after jsonb; append-only (revoke UPDATE/DELETE) |
| `handoffs` | id, conversation_id, reason, evidence jsonb, status, assigned_to, created_at |
| `llm_cache` | key_hash (PK), provider, model, request jsonb, response jsonb, tokens_in, tokens_out, created_at |
| `llm_calls` | id, conversation_id, step, cache_hit, provider, model, tokens_in, tokens_out, model_ms, status, error_class |
| `tool_calls` | id, conversation_id, step, tool, args jsonb, result_summary, tool_ms, status |
| `retrieval_logs` | id, conversation_id, query, fts_hits, vector_hits, fused jsonb, embedding_model |

The `vector(384)` dimension follows from bge-small-en-v1.5 / all-MiniLM-L6-v2 (both 384). Changing models means a migration and a re-embed, and `embedding_model` is checked at startup against configuration.

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
- `FakeAdapter`: reads fixtures from `tests/fixtures/llm/`, keyed by `sha256(canonical(request))`. A missing fixture raises `FixtureMissing` loudly (never invents output). A `RECORD=1` mode, run manually with a real provider, writes new fixtures.
- Selection: `LLM_PROVIDER=groq|gemini|fake`, `LLM_MODEL=<id>`; model names appear only in `.env` and adapter defaults. A test greps the tree for vendor imports and model-name strings outside the adapter package and `.env.example`.

### 4.3 Rate limiting and retry (`app/llm/ratelimit.py`)
- Token buckets per provider for requests/min and tokens/min (estimated from prompt size before the call, corrected from usage after). Limits come from config because free-tier values change.
- A daily request counter persisted in Postgres (survives restarts); at the configured cap, calls fail fast with `QuotaExhausted`.
- On HTTP 429 or 5xx: exponential backoff with full jitter, honoring `Retry-After`, capped at N attempts (default 4) and a total wall-time budget. Beyond that, raise `RateLimited`. Daily-quota 429s are distinguished from per-minute ones and are not retried.
- Workflow mapping: any of these errors during intent classification gives a "service busy, try again" with state `FAILED`/`PENDING` and handoff offer; during explanation, the deterministic decision is still shown using a templated (non-model) message. No path produces a success claim.

### 4.4 Cache (`app/llm/cache.py`)
Key = `sha256(provider, model, canonical full prompt, temperature, tool_schema_version)`. Stored in `llm_cache`. Modes: `read_write` (default), `replay_only` (miss raises `CacheMiss`, used by eval replays so reruns cost zero calls and are identical), `off`. Each lookup records hit/miss in `llm_calls` and the trace; eval reports include cache-hit rate. Responses with errors or invalid structure are not cached.

### 4.5 Request path
```
route -> workflow state machine -> LLMClient
LLMClient: cache lookup -> [hit: return] -> rate limiter -> adapter -> retry/backoff
           -> validate structured output -> cache write -> usage/latency logging -> return
```
Order matters: cache sits **outside** the limiter, so hits consume no quota. The `FakeAdapter` sits at the adapter position, so the cache, limiter, and validation logic are all exercised in CI without network.

### 4.6 Embeddings
`app/retrieval/embedder.py` wraps sentence-transformers (CPU), pinned name and revision hash, loaded once per process. Not part of the LLM provider interface and not a vendor LLM API. Documents are re-embedded only when `content_hash` or model id changes.

## 5. Cross-cutting design notes

1. **Authorization:** FastAPI dependency `current_customer` is the only source of customer ID. Tool functions take a `CustomerContext` created from it. Model tool-call arguments are validated against schemas that have no customer-ID field.
2. **Injection resistance:** policy chunks and customer text are placed in delimited data blocks, labeled untrusted; model outputs can only select from enumerated intents/fields. Backend ignores any "permission" inferred from text. Messages are scanned for obvious injection patterns for logging/flagging only (detection is not the defense; the architecture is).
3. **Idempotency:** client-supplied `Idempotency-Key` header on the execute endpoint plus the server-derived key in `actions`; repeat with same key returns the original result; same key with different payload returns 409. Unique constraints are the backstop.
4. **Timeout handling:** the (simulated) payments/fulfillment API call is made with an idempotency key; on timeout the action stays `pending`, and a reconciliation step queries by key. Never reported as success until confirmed.
5. **Policy applicability:** retrieval filters `policy_documents` by `effective_from <= reference_date < effective_to`, where reference_date is the order's purchase date (refund/shipping) or subscription start (cancellation). Rules in code carry the policy version they implement, and a test asserts code version matches the stored policy version.
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
│   │   ├── api/             routers: auth, chat, orders, subscriptions, proposals, handoffs
│   │   ├── auth/            sessions, current_customer dependency
│   │   ├── domain/          models, rules/ (refund, cancel, shipping), money
│   │   ├── workflow/        state machine, orchestrator, tool registry, caps
│   │   ├── llm/             base.py, client.py, ratelimit.py, cache.py, adapters/
│   │   ├── retrieval/       chunking, embedder, fts, vector, fusion, citations
│   │   ├── observability/   tracing, cost
│   │   └── db/              session, repositories
│   ├── seed/                synthetic data generator (deterministic seed)
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
