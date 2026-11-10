"""Conversations, decisions, confirmation tokens, actions, refunds, audit log.

Revision ID: 0002
Revises: 0001

Every guarantee here is enforced by the database (constraints and triggers), not by
application code that could be bypassed or reordered:
- idempotency: UNIQUE index on actions.idempotency_key
- single-use confirmation tokens: guard trigger + conditional UPDATE in the app
- append-only audit_log / decisions / refunds: triggers that raise
- Decision written before any action: actions.decision_id NOT NULL + composite FK + trigger
- paid-amount invariant: orders CHECK + refunds INSERT trigger maintaining the running total
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

UPGRADE_SQL = """
-- Generic guards -------------------------------------------------------------------
CREATE FUNCTION forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION '% is append-only: % is not allowed', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END
$fn$;

-- Conversations ----------------------------------------------------------------------
CREATE TABLE conversations (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id     uuid NOT NULL REFERENCES customers (id),
    state           text NOT NULL DEFAULT 'NEW' CHECK (state IN (
        'NEW', 'AUTHENTICATED', 'INTENT_CLASSIFIED', 'POLICY_RETRIEVED', 'DATA_FETCHED',
        'DECIDED', 'ANSWERED', 'DENIED_EXPLAINED', 'AWAITING_CONFIRMATION', 'EXECUTING',
        'COMPLETED', 'PENDING', 'FAILED', 'CANCELLED', 'HANDED_OFF')),
    tool_calls_used integer NOT NULL DEFAULT 0 CHECK (tool_calls_used >= 0),
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT conversations_id_customer_uq UNIQUE (id, customer_id)
);
CREATE INDEX conversations_customer_idx ON conversations (customer_id);

CREATE TABLE messages (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id   uuid NOT NULL,
    customer_id       uuid NOT NULL,
    role              text NOT NULL CHECK (role IN ('customer', 'assistant', 'system')),
    content           text NOT NULL,
    -- Prompt Guard annotation only; never a trust signal at read time.
    injection_verdict text NOT NULL DEFAULT 'unscanned'
                      CHECK (injection_verdict IN ('unscanned', 'clean', 'flagged')),
    created_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT messages_owner_fk FOREIGN KEY (conversation_id, customer_id)
        REFERENCES conversations (id, customer_id)
);
CREATE INDEX messages_conversation_idx ON messages (conversation_id, created_at);

-- Decisions: first-class, immutable, written before any action ---------------------------
CREATE TABLE decisions (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id        uuid NOT NULL,
    customer_id            uuid NOT NULL,
    action_type            text NOT NULL CHECK (action_type IN ('refund', 'cancel_subscription')),
    order_id               uuid,
    subscription_id        uuid,
    outcome                text NOT NULL
                           CHECK (outcome IN ('allowed', 'allowed_with_conditions', 'denied', 'needs_human')),
    amount_cents           bigint NOT NULL DEFAULT 0
                           CONSTRAINT decisions_amount_nonneg CHECK (amount_cents >= 0),
    reason_codes           text[] NOT NULL,
    rule_set_version       text NOT NULL,
    -- The live records the decision was computed from, snapshotted for audit and re-validation.
    input_state            jsonb NOT NULL,
    supersedes_decision_id uuid REFERENCES decisions (id),
    created_at             timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT decisions_target_present CHECK (
        (action_type = 'refund' AND order_id IS NOT NULL)
        OR (action_type = 'cancel_subscription' AND subscription_id IS NOT NULL)),
    CONSTRAINT decisions_amount_only_if_permitted CHECK (
        outcome IN ('allowed', 'allowed_with_conditions') OR amount_cents = 0),
    CONSTRAINT decisions_id_customer_uq UNIQUE (id, customer_id),
    CONSTRAINT decisions_conversation_owner_fk FOREIGN KEY (conversation_id, customer_id)
        REFERENCES conversations (id, customer_id),
    CONSTRAINT decisions_order_owner_fk FOREIGN KEY (order_id, customer_id)
        REFERENCES orders (id, customer_id),
    CONSTRAINT decisions_subscription_owner_fk FOREIGN KEY (subscription_id, customer_id)
        REFERENCES subscriptions (id, customer_id)
);
CREATE INDEX decisions_conversation_idx ON decisions (conversation_id);

CREATE TABLE decision_policies (
    decision_id        uuid NOT NULL REFERENCES decisions (id),
    policy_document_id uuid NOT NULL REFERENCES policy_documents (id),
    PRIMARY KEY (decision_id, policy_document_id)
);

CREATE TRIGGER decisions_immutable BEFORE UPDATE OR DELETE ON decisions
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER decisions_no_truncate BEFORE TRUNCATE ON decisions
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER decision_policies_immutable BEFORE UPDATE OR DELETE ON decision_policies
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER decision_policies_no_truncate BEFORE TRUNCATE ON decision_policies
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- Confirmation tokens: single use, expiring, bound to customer AND decision ----------------
CREATE TABLE confirmation_tokens (
    -- sha256 hex of the token; the raw token is never stored.
    token_hash  text PRIMARY KEY CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    customer_id uuid NOT NULL,
    decision_id uuid NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    used_at     timestamptz,
    CONSTRAINT confirmation_tokens_expiry CHECK (expires_at > created_at),
    CONSTRAINT confirmation_tokens_decision_owner_fk FOREIGN KEY (decision_id, customer_id)
        REFERENCES decisions (id, customer_id)
);
CREATE INDEX confirmation_tokens_decision_idx ON confirmation_tokens (decision_id);

CREATE FUNCTION confirmation_tokens_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'confirmation_tokens rows cannot be deleted' USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.used_at IS NOT NULL THEN
        RAISE EXCEPTION 'confirmation token already used' USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.token_hash IS DISTINCT FROM OLD.token_hash
       OR NEW.customer_id IS DISTINCT FROM OLD.customer_id
       OR NEW.decision_id IS DISTINCT FROM OLD.decision_id
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.expires_at IS DISTINCT FROM OLD.expires_at THEN
        RAISE EXCEPTION 'only used_at may change on a confirmation token' USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.used_at IS NULL THEN
        RAISE EXCEPTION 'used_at cannot be cleared' USING ERRCODE = 'restrict_violation';
    END IF;
    -- clock_timestamp(), not now(): now() is frozen at transaction start, so a long
    -- execution transaction would otherwise never see a token expire.
    IF clock_timestamp() >= OLD.expires_at THEN
        RAISE EXCEPTION 'confirmation token expired' USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER confirmation_tokens_guard_trg BEFORE UPDATE OR DELETE ON confirmation_tokens
    FOR EACH ROW EXECUTE FUNCTION confirmation_tokens_guard();

-- Actions: exactly one per decision, idempotent by key -------------------------------------
CREATE TABLE actions (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    decision_id     uuid NOT NULL,
    customer_id     uuid NOT NULL,
    action_type     text NOT NULL CHECK (action_type IN ('refund', 'cancel_subscription')),
    idempotency_key text NOT NULL CHECK (idempotency_key <> ''),
    amount_cents    bigint NOT NULL CONSTRAINT actions_amount_nonneg CHECK (amount_cents >= 0),
    status          text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'succeeded', 'failed')),
    result          jsonb,
    downstream_ref  text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    completed_at    timestamptz,
    CONSTRAINT actions_completed_when_final CHECK (status = 'pending' OR completed_at IS NOT NULL),
    CONSTRAINT actions_id_customer_uq UNIQUE (id, customer_id),
    CONSTRAINT actions_decision_uq UNIQUE (decision_id),
    CONSTRAINT actions_decision_owner_fk FOREIGN KEY (decision_id, customer_id)
        REFERENCES decisions (id, customer_id)
);
CREATE UNIQUE INDEX actions_idempotency_key_uq ON actions (idempotency_key);

CREATE FUNCTION actions_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    d decisions%ROWTYPE;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'actions cannot be deleted' USING ERRCODE = 'restrict_violation';
    END IF;
    IF TG_OP = 'INSERT' THEN
        SELECT * INTO d FROM decisions WHERE id = NEW.decision_id;
        IF d.outcome NOT IN ('allowed', 'allowed_with_conditions') THEN
            RAISE EXCEPTION 'decision % does not permit an action (outcome %)', d.id, d.outcome
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.action_type <> d.action_type OR NEW.amount_cents <> d.amount_cents THEN
            RAISE EXCEPTION 'action type or amount differs from decision %', d.id
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.status <> 'pending' THEN
            RAISE EXCEPTION 'actions must be created pending' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    -- UPDATE: only pending -> succeeded/failed, and only result fields may change.
    IF OLD.status <> 'pending' THEN
        RAISE EXCEPTION 'action % is final (%)', OLD.id, OLD.status USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.decision_id IS DISTINCT FROM OLD.decision_id
       OR NEW.customer_id IS DISTINCT FROM OLD.customer_id
       OR NEW.action_type IS DISTINCT FROM OLD.action_type
       OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
       OR NEW.amount_cents IS DISTINCT FROM OLD.amount_cents
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'only status/result/downstream_ref/completed_at may change on an action'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER actions_guard_trg BEFORE INSERT OR UPDATE OR DELETE ON actions
    FOR EACH ROW EXECUTE FUNCTION actions_guard();

-- Refunds: immutable; INSERT maintains the order's running total ----------------------------
CREATE TABLE refunds (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    action_id    uuid NOT NULL,
    order_id     uuid NOT NULL,
    customer_id  uuid NOT NULL,
    amount_cents bigint NOT NULL CONSTRAINT refunds_amount_positive CHECK (amount_cents > 0),
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT refunds_action_uq UNIQUE (action_id),
    CONSTRAINT refunds_action_owner_fk FOREIGN KEY (action_id, customer_id)
        REFERENCES actions (id, customer_id),
    CONSTRAINT refunds_order_owner_fk FOREIGN KEY (order_id, customer_id)
        REFERENCES orders (id, customer_id)
);
CREATE INDEX refunds_order_idx ON refunds (order_id);

CREATE FUNCTION refunds_apply() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    a actions%ROWTYPE;
    d decisions%ROWTYPE;
BEGIN
    SELECT * INTO a FROM actions WHERE id = NEW.action_id;
    SELECT * INTO d FROM decisions WHERE id = a.decision_id;
    IF d.order_id IS DISTINCT FROM NEW.order_id OR a.amount_cents <> NEW.amount_cents THEN
        RAISE EXCEPTION 'refund must match its action and decision (order and amount)'
            USING ERRCODE = 'check_violation';
    END IF;
    -- The UPDATE takes the order row lock; orders_refund_within_paid rejects any overshoot.
    UPDATE orders SET refunded_total_cents = refunded_total_cents + NEW.amount_cents
    WHERE id = NEW.order_id AND customer_id = NEW.customer_id;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER refunds_apply_trg AFTER INSERT ON refunds
    FOR EACH ROW EXECUTE FUNCTION refunds_apply();
CREATE TRIGGER refunds_immutable BEFORE UPDATE OR DELETE ON refunds
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER refunds_no_truncate BEFORE TRUNCATE ON refunds
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- Drift detection: denormalized total vs the sum of refund records -------------------------
CREATE VIEW v_refund_drift AS
SELECT o.id AS order_id,
       o.refunded_total_cents AS recorded_cents,
       COALESCE(SUM(r.amount_cents), 0)::bigint AS summed_cents
FROM orders o
LEFT JOIN refunds r ON r.order_id = o.id
GROUP BY o.id, o.refunded_total_cents
HAVING o.refunded_total_cents <> COALESCE(SUM(r.amount_cents), 0);

-- Audit log: append-only ----------------------------------------------------------------------
CREATE TABLE audit_log (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at          timestamptz NOT NULL DEFAULT now(),
    actor_type  text NOT NULL CHECK (actor_type IN ('customer', 'system', 'human')),
    actor_id    uuid,
    customer_id uuid REFERENCES customers (id),
    event       text NOT NULL,
    entity_type text NOT NULL,
    entity_id   text NOT NULL,
    before      jsonb,
    after       jsonb
);
CREATE INDEX audit_log_entity_idx ON audit_log (entity_type, entity_id);
CREATE TRIGGER audit_log_immutable BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();
"""

DOWNGRADE_SQL = """
DROP VIEW v_refund_drift;
DROP TABLE audit_log, refunds, actions, confirmation_tokens, decision_policies, decisions,
           messages, conversations;
DROP FUNCTION refunds_apply(), actions_guard(), confirmation_tokens_guard(), forbid_mutation();
"""


def upgrade() -> None:
    # psycopg treats % as a placeholder marker, so literal percent signs (RAISE formats,
    # %ROWTYPE) are doubled before being sent.
    op.get_bind().exec_driver_sql(UPGRADE_SQL.replace("%", "%%"))


def downgrade() -> None:
    op.get_bind().exec_driver_sql(DOWNGRADE_SQL)
