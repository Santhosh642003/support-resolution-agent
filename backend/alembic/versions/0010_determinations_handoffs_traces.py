"""Determinations on the decisions table, applied clause keys, handoffs and turn traces.

Revision ID: 0010
Revises: 0009

- An order-status determination has the SAME SHAPE as a refund or cancellation Decision (applied
  clause keys, input state, reason codes, rule-set version) but authorises no action. So it is a
  row in `decisions` with action_type 'order_status' and outcome 'informational'. The existing
  trigger that lets an action reference only an 'allowed' decision already makes it impossible to
  act on one (tested). `finding` is the domain-specific result of the rules ('presumed_lost',
  'delivered', ...); the M2 paths will use it too.
- decision_clauses: the exact clause keys a Decision applied. They ARE the citations (SPEC 10.4).
  decision_policies links a decision to policy DOCUMENTS; this closes the gap to clause level.
  Immutable, owner-checked by a composite foreign key.
- handoffs: unresolved cases (kind 'handoff') and non-blocking review flags (kind 'review') with
  the evidence gathered. Append-only.
- turn_traces: the full trace of every turn (scope decisions, retrieval, determination, both model
  calls, Prompt Guard, fallbacks). Append-only.
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

UPGRADE_SQL = """
ALTER TABLE decisions DROP CONSTRAINT decisions_action_type_check;
ALTER TABLE decisions ADD CONSTRAINT decisions_action_type_check
    CHECK (action_type IN ('refund', 'cancel_subscription', 'order_status'));
ALTER TABLE decisions DROP CONSTRAINT decisions_outcome_check;
ALTER TABLE decisions ADD CONSTRAINT decisions_outcome_check
    CHECK (outcome IN ('allowed', 'allowed_with_conditions', 'denied', 'needs_human', 'informational'));
ALTER TABLE decisions DROP CONSTRAINT decisions_target_present;
ALTER TABLE decisions ADD CONSTRAINT decisions_target_present CHECK (
    (action_type IN ('refund', 'order_status') AND order_id IS NOT NULL)
    OR (action_type = 'cancel_subscription' AND subscription_id IS NOT NULL));
ALTER TABLE decisions ADD COLUMN finding text;
ALTER TABLE decisions ADD CONSTRAINT decisions_status_shape CHECK (
    action_type <> 'order_status'
    OR (finding IS NOT NULL AND outcome IN ('informational', 'needs_human')));

CREATE TABLE decision_clauses (
    decision_id uuid NOT NULL,
    customer_id uuid NOT NULL,
    chunk_key   text NOT NULL REFERENCES policy_chunks (chunk_key),
    PRIMARY KEY (decision_id, chunk_key),
    CONSTRAINT decision_clauses_owner_fk FOREIGN KEY (decision_id, customer_id)
        REFERENCES decisions (id, customer_id)
);
CREATE TRIGGER decision_clauses_immutable BEFORE UPDATE OR DELETE ON decision_clauses
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER decision_clauses_no_truncate BEFORE TRUNCATE ON decision_clauses
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

CREATE TABLE handoffs (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL,
    customer_id     uuid NOT NULL,
    kind            text NOT NULL CHECK (kind IN ('handoff', 'review')),
    reason_code     text NOT NULL,
    evidence        jsonb NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT handoffs_owner_fk FOREIGN KEY (conversation_id, customer_id)
        REFERENCES conversations (id, customer_id)
);
CREATE INDEX handoffs_conversation_idx ON handoffs (conversation_id);
CREATE TRIGGER handoffs_immutable BEFORE UPDATE OR DELETE ON handoffs
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER handoffs_no_truncate BEFORE TRUNCATE ON handoffs
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

CREATE TABLE turn_traces (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL,
    customer_id     uuid NOT NULL,
    turn_no         integer NOT NULL CHECK (turn_no >= 1),
    final_state     text NOT NULL,
    tool_calls      integer NOT NULL CHECK (tool_calls >= 0),
    model_calls     integer NOT NULL CHECK (model_calls BETWEEN 0 AND 2),
    trace           jsonb NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT turn_traces_turn_uq UNIQUE (conversation_id, turn_no),
    CONSTRAINT turn_traces_owner_fk FOREIGN KEY (conversation_id, customer_id)
        REFERENCES conversations (id, customer_id)
);
CREATE TRIGGER turn_traces_immutable BEFORE UPDATE OR DELETE ON turn_traces
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER turn_traces_no_truncate BEFORE TRUNCATE ON turn_traces
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();
"""

DOWNGRADE_SQL = """
DROP TABLE turn_traces, handoffs, decision_clauses;
ALTER TABLE decisions DROP CONSTRAINT decisions_status_shape;
ALTER TABLE decisions DROP COLUMN finding;
ALTER TABLE decisions DROP CONSTRAINT decisions_target_present;
ALTER TABLE decisions ADD CONSTRAINT decisions_target_present CHECK (
    (action_type = 'refund' AND order_id IS NOT NULL)
    OR (action_type = 'cancel_subscription' AND subscription_id IS NOT NULL));
ALTER TABLE decisions DROP CONSTRAINT decisions_outcome_check;
ALTER TABLE decisions ADD CONSTRAINT decisions_outcome_check
    CHECK (outcome IN ('allowed', 'allowed_with_conditions', 'denied', 'needs_human'));
ALTER TABLE decisions DROP CONSTRAINT decisions_action_type_check;
ALTER TABLE decisions ADD CONSTRAINT decisions_action_type_check
    CHECK (action_type IN ('refund', 'cancel_subscription'));
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(UPGRADE_SQL.replace("%", "%%"))


def downgrade() -> None:
    op.get_bind().exec_driver_sql(DOWNGRADE_SQL)
