"""LLM response cache, per-call trace log, and the persisted daily request counter.

Revision ID: 0005
Revises: 0004

- llm_cache: keyed by a sha256 of everything that can change the output (provider, model, full
  prompt, temperature, tool schema and version, prompt template version, ...). Entries are
  IMMUTABLE once written: a cached response is a snapshot of ONE sampling, and replays must
  return exactly that, never a quietly replaced one. (Deleting an entry is allowed: eviction.)
  The prompt itself is not stored, only its hash, so no customer text is duplicated here.
- llm_calls: one row per logical call with cache hit/miss, outcome, token counts and the split
  of model time versus total time. Integers only (milliseconds), per the schema's no-float rule.
- llm_usage_daily: provider quota counter, one row per (provider, UTC day). The increment is a
  conditional UPSERT (app/providers/ratelimit.py), so concurrent processes cannot overspend.
These tables are not customer-scoped (no customer_id); they hold no customer data.
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

UPGRADE_SQL = """
CREATE TABLE llm_cache (
    cache_key               text PRIMARY KEY,
    provider                text NOT NULL,
    model                   text NOT NULL,
    kind                    text NOT NULL CHECK (kind IN ('complete', 'structured')),
    prompt_template_version text NOT NULL,
    tool_schema_version     text NOT NULL,
    response                jsonb NOT NULL,
    adapter_calls           integer NOT NULL CHECK (adapter_calls > 0),
    created_at              timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER llm_cache_no_update BEFORE UPDATE ON llm_cache
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE TABLE llm_usage_daily (
    provider text    NOT NULL,
    day      date    NOT NULL,
    requests integer NOT NULL CHECK (requests >= 0),
    PRIMARY KEY (provider, day)
);

CREATE TABLE llm_calls (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    trace_id           text NOT NULL,
    scenario_id        text,
    provider           text NOT NULL,
    model              text NOT NULL,
    cache_status       text NOT NULL CHECK (cache_status IN ('hit', 'miss', 'off', 'replay_miss')),
    outcome            text NOT NULL CHECK (outcome IN ('ok', 'error')),
    error_kind         text,
    adapter_calls      integer NOT NULL DEFAULT 0,
    transport_attempts integer NOT NULL DEFAULT 0,
    prompt_tokens      integer,
    completion_tokens  integer,
    model_ms           integer NOT NULL DEFAULT 0,
    total_ms           integer NOT NULL DEFAULT 0,
    created_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT llm_calls_error_has_kind CHECK ((outcome = 'error') = (error_kind IS NOT NULL))
);
CREATE INDEX llm_calls_trace_idx ON llm_calls (trace_id);
"""

DOWNGRADE_SQL = """
DROP TABLE llm_calls, llm_usage_daily, llm_cache;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(UPGRADE_SQL.replace("%", "%%"))


def downgrade() -> None:
    op.get_bind().exec_driver_sql(DOWNGRADE_SQL)
