"""Row-level security and the application role's grants (SPEC 17).

Revision ID: 0012
Revises: 0011

Customer isolation becomes a property the DATABASE enforces, independent of the repository filters:
a query with a missing WHERE returns only the current customer's rows, and an insert for another
customer is refused. Every protected table gets ENABLE and FORCE ROW LEVEL SECURITY (FORCE so that
even the owner is subject to the policy: a future ownership mistake fails closed), and ONE policy
keyed on the transaction-scoped setting `app.current_customer_id`.

The four ways RLS silently does nothing, and where each is handled:
  1. superuser / table owner bypass  -> the app connects as support_app, which is neither
     (roles.sql creates it; this migration REFUSES to run if it does not exist);
  2. owner bypass via a future ownership change -> FORCE ROW LEVEL SECURITY on every table;
  3. session-scoped state leaking through a pool -> the setting is set with set_config(.., true), i.e.
     SET LOCAL, inside the one transaction that wraps the request (app/api/deps.py);
  4. tests that pass as superuser -> every RLS test connects as support_app (tests/db/test_rls.py).

Grants are explicit and minimal: the app can READ the tables the workflow reads and WRITE only the
record of the conversation. It holds no write on any business table; the M2 write paths add theirs
in their own migrations, next to the code that needs them.

Policies are TO PUBLIC (every role without BYPASSRLS), so a role added later is isolated by default.
Consequence to remember: a DATA migration that must see every customer's rows cannot run as
support_owner (FORCE applies to it); run it as the bootstrap superuser.
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

# Tables keyed on customer_id (the policy column is customer_id).
CUSTOMER_TABLES = (
    "actions",
    "audit_log",
    "auth_sessions",
    "confirmation_tokens",
    "conversations",
    "decision_clauses",
    "decisions",
    "handoffs",
    "messages",
    "order_items",
    "orders",
    "refund_lines",
    "refunds",
    "shipment_events",
    "shipments",
    "subscriptions",
    "turn_traces",
)

UPGRADE_SQL = """
DO $guard$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'support_app') THEN
        RAISE EXCEPTION 'role support_app does not exist: run python -m app.db.bootstrap first (SPEC 17)';
    END IF;
END
$guard$;

-- The current customer for this transaction, or NULL. NULLIF: after a transaction that used the
-- setting ends, a pooled connection reports '' (not NULL) and ''::uuid would raise.
CREATE FUNCTION app_current_customer() RETURNS uuid LANGUAGE sql STABLE PARALLEL SAFE AS
$fn$ SELECT NULLIF(current_setting('app.current_customer_id', true), '')::uuid $fn$;

-- The session id the caller presented, used ONLY to look that one session up (the customer is not
-- known until it is resolved).
CREATE FUNCTION app_presented_session() RETURNS uuid LANGUAGE sql STABLE PARALLEL SAFE AS
$fn$ SELECT NULLIF(current_setting('app.session_id', true), '')::uuid $fn$;

GRANT EXECUTE ON FUNCTION app_current_customer(), app_presented_session() TO support_app;
"""

POLICY_SQL = """
ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {t} FORCE ROW LEVEL SECURITY;
CREATE POLICY customer_isolation ON {t} FOR ALL
    USING (customer_id = (SELECT app_current_customer()))
    WITH CHECK (customer_id = (SELECT app_current_customer()));
"""

SPECIAL_POLICIES_SQL = """
-- customers is keyed on id.
ALTER TABLE customers ENABLE ROW LEVEL SECURITY;
ALTER TABLE customers FORCE ROW LEVEL SECURITY;
CREATE POLICY customer_isolation ON customers FOR ALL
    USING (id = (SELECT app_current_customer()))
    WITH CHECK (id = (SELECT app_current_customer()));

-- decision_policies has no customer_id; it is visible exactly when its decision is (the subselect is
-- itself filtered by the decisions policy).
ALTER TABLE decision_policies ENABLE ROW LEVEL SECURITY;
ALTER TABLE decision_policies FORCE ROW LEVEL SECURITY;
CREATE POLICY customer_isolation ON decision_policies FOR ALL
    USING (EXISTS (SELECT 1 FROM decisions d WHERE d.id = decision_policies.decision_id))
    WITH CHECK (EXISTS (SELECT 1 FROM decisions d WHERE d.id = decision_policies.decision_id));

-- A session row is also readable by the id the caller presented (read-only), so the customer can be
-- resolved before it is known. It reveals one row to someone who already holds that id.
CREATE POLICY session_lookup ON auth_sessions FOR SELECT
    USING (id = (SELECT app_presented_session()));
"""

VIEWS_SQL = """
-- A view runs with its OWNER's privileges by default, and the owner is subject to FORCE ROW LEVEL
-- SECURITY, so even a superuser reading the drift views would see only the owner's (empty) scope
-- and the invariant check would silently report "no drift". security_invoker makes a view evaluate
-- as the CALLER: the superuser audit sees everything, the app role sees only its customer.
ALTER VIEW v_refund_drift SET (security_invoker = true);
ALTER VIEW v_refund_line_drift SET (security_invoker = true);
"""

GRANTS_SQL = """
-- Read: what the workflow and the read API read.
GRANT SELECT ON customers, subscriptions, orders, order_items, shipments, shipment_events,
    actions, refunds, refund_lines, confirmation_tokens,
    products, policy_documents, policy_chunks, policy_incorporations TO support_app;
-- The record of the conversation (append-only tables: no UPDATE, no DELETE).
GRANT SELECT, INSERT ON conversations, messages, decisions, decision_clauses, decision_policies,
    handoffs, turn_traces, audit_log, auth_sessions TO support_app;
GRANT UPDATE (state, tool_calls_used) ON conversations TO support_app;
GRANT UPDATE (revoked_at) ON auth_sessions TO support_app;
-- Provider layer: response cache, daily quota counters, per-call diagnostics (not customer data).
GRANT SELECT, INSERT ON llm_cache TO support_app;
GRANT SELECT, INSERT, UPDATE ON llm_usage_daily TO support_app;
GRANT INSERT ON llm_calls TO support_app;
GRANT USAGE ON SEQUENCE audit_log_id_seq, llm_calls_id_seq TO support_app;
"""

DOWNGRADE_SQL = """
ALTER VIEW v_refund_drift RESET (security_invoker);
ALTER VIEW v_refund_line_drift RESET (security_invoker);
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM support_app;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM support_app;
{drops}
DROP POLICY session_lookup ON auth_sessions;
DROP FUNCTION app_presented_session();
DROP FUNCTION app_current_customer();
"""


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(UPGRADE_SQL)
    for table in CUSTOMER_TABLES:
        bind.exec_driver_sql(POLICY_SQL.format(t=table))
    bind.exec_driver_sql(SPECIAL_POLICIES_SQL)
    bind.exec_driver_sql(VIEWS_SQL)
    bind.exec_driver_sql(GRANTS_SQL)


def downgrade() -> None:
    drops = "\n".join(
        f"DROP POLICY customer_isolation ON {t};\n"
        f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY;\nALTER TABLE {t} DISABLE ROW LEVEL SECURITY;"
        for t in (*CUSTOMER_TABLES, "customers", "decision_policies")
    )
    op.get_bind().exec_driver_sql(DOWNGRADE_SQL.format(drops=drops))
