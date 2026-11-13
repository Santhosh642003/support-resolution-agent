"""Close the RLS gaps: audit_log's NULL customer, llm_cache and llm_calls (SPEC 17.7).

Revision ID: 0013
Revises: 0012

1. audit_log: the app role may insert a row only for the CURRENT customer. NULL is not writable by
   the app role. Before this the same result followed implicitly (NULL = x is not true); the policy
   now says it, so the rule survives a rewrite of the expression. A system event with no customer
   is out of scope for the application path; if one is ever needed it goes through a separate role
   and a separate explicit decision.
2. llm_cache: gets a nullable customer_id. A row belongs to the customer whose message and order data
   are in the prompt; NULL means "the prompt held no customer data" and the row is shared. Reads see
   own rows and shared rows; writes may create own rows and shared rows. Rows that existed before
   this migration cannot be attributed (only a hash was stored) and stay NULL, i.e. shared.
3. llm_calls: error_detail is the provider's error body, and a provider error body can echo the
   prompt or the model's failed output, so it can hold customer text. Same column, and a stricter
   write rule: a shared (NULL) row may not carry an error body.
No foreign key on either new column, deliberately: these rows are written on their own connection,
not in the request's transaction, and the policy already pins the value to the authenticated
customer's id, so a foreign key would add coupling and no protection.
llm_usage_daily holds only (provider, day, requests): no text, left unprotected (test catalog).
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

UPGRADE_SQL = """
-- 1. audit_log: explicit NOT NULL in the write check.
DROP POLICY customer_isolation ON audit_log;
CREATE POLICY customer_isolation ON audit_log FOR ALL
    USING (customer_id = (SELECT app_current_customer()))
    WITH CHECK (customer_id IS NOT NULL AND customer_id = (SELECT app_current_customer()));

-- 2. llm_cache
ALTER TABLE llm_cache ADD COLUMN customer_id uuid;
CREATE INDEX llm_cache_customer_idx ON llm_cache (customer_id) WHERE customer_id IS NOT NULL;
ALTER TABLE llm_cache ENABLE ROW LEVEL SECURITY;
ALTER TABLE llm_cache FORCE ROW LEVEL SECURITY;
CREATE POLICY customer_isolation ON llm_cache FOR ALL
    USING (customer_id IS NULL OR customer_id = (SELECT app_current_customer()))
    WITH CHECK (customer_id IS NULL OR customer_id = (SELECT app_current_customer()));

-- 3. llm_calls
ALTER TABLE llm_calls ADD COLUMN customer_id uuid;
ALTER TABLE llm_calls ENABLE ROW LEVEL SECURITY;
ALTER TABLE llm_calls FORCE ROW LEVEL SECURITY;
CREATE POLICY customer_isolation ON llm_calls FOR ALL
    USING (customer_id IS NULL OR customer_id = (SELECT app_current_customer()))
    WITH CHECK (customer_id = (SELECT app_current_customer())
                OR (customer_id IS NULL AND error_detail IS NULL));
"""

DOWNGRADE_SQL = """
DROP POLICY customer_isolation ON llm_calls;
ALTER TABLE llm_calls NO FORCE ROW LEVEL SECURITY;
ALTER TABLE llm_calls DISABLE ROW LEVEL SECURITY;
ALTER TABLE llm_calls DROP COLUMN customer_id;
DROP POLICY customer_isolation ON llm_cache;
ALTER TABLE llm_cache NO FORCE ROW LEVEL SECURITY;
ALTER TABLE llm_cache DISABLE ROW LEVEL SECURITY;
DROP INDEX llm_cache_customer_idx;
ALTER TABLE llm_cache DROP COLUMN customer_id;
DROP POLICY customer_isolation ON audit_log;
CREATE POLICY customer_isolation ON audit_log FOR ALL
    USING (customer_id = (SELECT app_current_customer()))
    WITH CHECK (customer_id = (SELECT app_current_customer()));
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(UPGRADE_SQL.replace("%", "%%"))


def downgrade() -> None:
    op.get_bind().exec_driver_sql(DOWNGRADE_SQL)
