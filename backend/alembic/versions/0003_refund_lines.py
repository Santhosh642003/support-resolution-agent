"""Line-level refunds.

Revision ID: 0003
Revises: 0002

Three seeded policy rules are item-level (proration by items shipped, accessed digital
add-ons, final_sale items), and a second refund request must be able to tell what was already
refunded per item. Same pattern as the order level, one level down:
- refund_lines rows are immutable;
- order_items.refunded_cents is the denormalized running total, maintained ONLY by the
  refund_lines INSERT trigger, with CHECK (refunded_cents <= paid_cents);
- a deferred constraint trigger requires each refund header to equal the sum of its lines
  (and to have at least one line) at commit;
- orders.refunded_total_cents stays the aggregate and the enforcement point for the
  paid-amount invariant. Drift views check both levels agree.

Naming: the PM brief says "order_line_id"; the table is order_items, so the column is
order_item_id.
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

UPGRADE_SQL = """
ALTER TABLE order_items
    ADD COLUMN refunded_cents bigint NOT NULL DEFAULT 0
        CONSTRAINT order_items_refunded_nonneg CHECK (refunded_cents >= 0),
    ADD CONSTRAINT order_items_refunded_within_paid CHECK (refunded_cents <= paid_cents),
    ADD CONSTRAINT order_items_id_customer_uq UNIQUE (id, customer_id),
    ADD CONSTRAINT order_items_id_order_uq UNIQUE (id, order_id);

ALTER TABLE refunds
    ADD CONSTRAINT refunds_id_customer_uq UNIQUE (id, customer_id),
    ADD CONSTRAINT refunds_id_order_uq UNIQUE (id, order_id);

CREATE TABLE refund_lines (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    refund_id     uuid NOT NULL,
    order_item_id uuid NOT NULL,
    order_id      uuid NOT NULL,
    customer_id   uuid NOT NULL,
    amount_cents  bigint NOT NULL
                  CONSTRAINT refund_lines_amount_positive CHECK (amount_cents > 0),
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT refund_lines_refund_item_uq UNIQUE (refund_id, order_item_id),
    -- The refund and the item must belong to the SAME order and the SAME customer.
    CONSTRAINT refund_lines_refund_order_fk FOREIGN KEY (refund_id, order_id)
        REFERENCES refunds (id, order_id),
    CONSTRAINT refund_lines_refund_owner_fk FOREIGN KEY (refund_id, customer_id)
        REFERENCES refunds (id, customer_id),
    CONSTRAINT refund_lines_item_order_fk FOREIGN KEY (order_item_id, order_id)
        REFERENCES order_items (id, order_id),
    CONSTRAINT refund_lines_item_owner_fk FOREIGN KEY (order_item_id, customer_id)
        REFERENCES order_items (id, customer_id)
);
CREATE INDEX refund_lines_item_idx ON refund_lines (order_item_id);

CREATE FUNCTION refund_lines_apply() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    -- Takes the item row lock; order_items_refunded_within_paid rejects any overshoot.
    UPDATE order_items SET refunded_cents = refunded_cents + NEW.amount_cents
    WHERE id = NEW.order_item_id AND customer_id = NEW.customer_id;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER refund_lines_apply_trg AFTER INSERT ON refund_lines
    FOR EACH ROW EXECUTE FUNCTION refund_lines_apply();
CREATE TRIGGER refund_lines_immutable BEFORE UPDATE OR DELETE ON refund_lines
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER refund_lines_no_truncate BEFORE TRUNCATE ON refund_lines
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- A refund header must equal the sum of its lines, checked at commit.
CREATE FUNCTION refunds_lines_sum_check() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    line_sum bigint;
    line_count integer;
BEGIN
    SELECT COALESCE(SUM(amount_cents), 0), COUNT(*) INTO line_sum, line_count
    FROM refund_lines WHERE refund_id = NEW.id;
    IF line_count = 0 OR line_sum <> NEW.amount_cents THEN
        RAISE EXCEPTION 'refund % must equal the sum of its lines (header %, lines %, count %)',
            NEW.id, NEW.amount_cents, line_sum, line_count
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END
$fn$;
CREATE CONSTRAINT TRIGGER refunds_lines_sum_trg AFTER INSERT ON refunds
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION refunds_lines_sum_check();

-- Drift detection, line level and cross level.
CREATE VIEW v_refund_line_drift AS
SELECT 'item_total'::text AS level, i.id AS entity_id,
       i.refunded_cents AS recorded_cents,
       COALESCE(SUM(l.amount_cents), 0)::bigint AS summed_cents
FROM order_items i
LEFT JOIN refund_lines l ON l.order_item_id = i.id
GROUP BY i.id, i.refunded_cents
HAVING i.refunded_cents <> COALESCE(SUM(l.amount_cents), 0)
UNION ALL
SELECT 'order_vs_lines'::text, o.id,
       o.refunded_total_cents,
       COALESCE(SUM(l.amount_cents), 0)::bigint
FROM orders o
LEFT JOIN refund_lines l ON l.order_id = o.id
GROUP BY o.id, o.refunded_total_cents
HAVING o.refunded_total_cents <> COALESCE(SUM(l.amount_cents), 0);
"""

DOWNGRADE_SQL = """
DROP VIEW v_refund_line_drift;
DROP TRIGGER refunds_lines_sum_trg ON refunds;
DROP FUNCTION refunds_lines_sum_check();
DROP TABLE refund_lines;
DROP FUNCTION refund_lines_apply();
ALTER TABLE refunds DROP CONSTRAINT refunds_id_customer_uq, DROP CONSTRAINT refunds_id_order_uq;
ALTER TABLE order_items
    DROP CONSTRAINT order_items_id_customer_uq, DROP CONSTRAINT order_items_id_order_uq,
    DROP CONSTRAINT order_items_refunded_within_paid, DROP COLUMN refunded_cents;
"""


def upgrade() -> None:
    # psycopg treats % as a placeholder marker; double literal percent signs.
    op.get_bind().exec_driver_sql(UPGRADE_SQL.replace("%", "%%"))


def downgrade() -> None:
    op.get_bind().exec_driver_sql(DOWNGRADE_SQL)
