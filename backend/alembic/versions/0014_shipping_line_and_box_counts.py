"""Shipping as an order line, and item counts on subscription box lines (SPEC 18.6).

Revision ID: 0014
Revises: 0013

1. Shipping becomes an ORDER LINE with a type flag instead of only a header field. A lost shipment
   refunds the delivery fee it never delivered, and the refund machinery already works per line: the
   per-line cap (order_items_refunded_within_paid), the order-level paid-amount invariant, the refund
   header equal to the sum of its lines and the drift views all apply to a shipping line unchanged.
   Whether a shipping line is refundable (only when the ENTIRE shipment is lost) is a rule, in the
   rules engine; the database only guarantees the shape:
     * line_type is 'item' or 'shipping'; a shipping line has no product and an item always has one;
     * at most one shipping line per order (partial unique index);
     * a shipping line is never final_sale, never digital, never a box;
     * the shipping line's paid_cents equals orders.shipping_cents, and an order with shipping_cents
       > 0 has exactly that line (deferred constraint trigger, checked at commit from either side).
   orders.shipping_cents stays: orders_paid_consistent still reads it.
   Existing orders with shipping_cents > 0 get their line here (the backfill).
2. items_total / items_shipped on order_items: how many items a subscription box holds and how many
   have shipped, the input to the proration rule (refund v1 clause 4). Both or neither; for item
   lines only; 0 <= shipped <= total and total > 0.

Data migration under row-level security: orders and order_items are FORCE ROW LEVEL SECURITY, so even
the owner sees no rows without a current customer (SPEC 17, migration 0012). The backfill therefore
lifts FORCE on those two tables for its own statements and restores it before the migration ends;
both are DDL in this one transaction, so a failure leaves FORCE in place.
"""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

UPGRADE_SQL = """
ALTER TABLE order_items
    ADD COLUMN line_type    text NOT NULL DEFAULT 'item'
                            CONSTRAINT order_items_line_type_valid CHECK (line_type IN ('item', 'shipping')),
    ADD COLUMN items_total  integer,
    ADD COLUMN items_shipped integer,
    ALTER COLUMN product_id DROP NOT NULL,
    ADD CONSTRAINT order_items_product_by_type
        CHECK ((line_type = 'shipping') = (product_id IS NULL)),
    ADD CONSTRAINT order_items_shipping_plain
        CHECK (line_type = 'item'
               OR (NOT final_sale AND digital_accessed_at IS NULL AND items_total IS NULL)),
    ADD CONSTRAINT order_items_box_counts
        CHECK ((items_total IS NULL AND items_shipped IS NULL)
               OR (items_total IS NOT NULL AND items_shipped IS NOT NULL  -- a NULL would pass a CHECK
                   AND items_total > 0 AND items_shipped >= 0 AND items_shipped <= items_total));

CREATE UNIQUE INDEX order_items_one_shipping_line_uq ON order_items (order_id)
    WHERE line_type = 'shipping';

-- Backfill (see the docstring: FORCE is lifted for these statements only).
ALTER TABLE orders NO FORCE ROW LEVEL SECURITY;
ALTER TABLE order_items NO FORCE ROW LEVEL SECURITY;
INSERT INTO order_items (order_id, customer_id, line_type, qty, unit_price_cents, paid_cents)
SELECT id, customer_id, 'shipping', 1, shipping_cents, shipping_cents
FROM orders WHERE shipping_cents > 0;
ALTER TABLE order_items FORCE ROW LEVEL SECURITY;
ALTER TABLE orders FORCE ROW LEVEL SECURITY;

CREATE FUNCTION order_shipping_line_check() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    target   uuid;
    header   bigint;
    line_sum bigint;
BEGIN
    IF TG_TABLE_NAME = 'orders' THEN
        target := NEW.id;
    ELSE
        target := NEW.order_id;
    END IF;
    SELECT shipping_cents INTO header FROM orders WHERE id = target;
    SELECT COALESCE(SUM(paid_cents), 0) INTO line_sum
    FROM order_items WHERE order_id = target AND line_type = 'shipping';
    IF header IS NOT NULL AND header <> line_sum THEN
        RAISE EXCEPTION 'order % shipping_cents (%) must equal its shipping line (%)',
            target, header, line_sum USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END
$fn$;
CREATE CONSTRAINT TRIGGER order_items_shipping_line_trg
    AFTER INSERT OR UPDATE OF paid_cents, line_type ON order_items
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION order_shipping_line_check();
CREATE CONSTRAINT TRIGGER orders_shipping_line_trg
    AFTER INSERT OR UPDATE OF shipping_cents ON orders
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION order_shipping_line_check();
"""

DOWNGRADE_SQL = """
DROP TRIGGER orders_shipping_line_trg ON orders;
DROP TRIGGER order_items_shipping_line_trg ON order_items;
DROP FUNCTION order_shipping_line_check();
ALTER TABLE order_items NO FORCE ROW LEVEL SECURITY;
DELETE FROM order_items WHERE line_type = 'shipping';
ALTER TABLE order_items FORCE ROW LEVEL SECURITY;
DROP INDEX order_items_one_shipping_line_uq;
ALTER TABLE order_items
    DROP CONSTRAINT order_items_box_counts,
    DROP CONSTRAINT order_items_shipping_plain,
    DROP CONSTRAINT order_items_product_by_type,
    ALTER COLUMN product_id SET NOT NULL,
    DROP COLUMN items_shipped,
    DROP COLUMN items_total,
    DROP COLUMN line_type;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(UPGRADE_SQL.replace("%", "%%"))


def downgrade() -> None:
    op.get_bind().exec_driver_sql(DOWNGRADE_SQL)
