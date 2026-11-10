"""Core domain: customers, orders, subscriptions, shipments, policies.

Revision ID: 0001
Revises:

Design notes (all data is SIMULATED):
- Money is BIGINT cents everywhere, with CHECKs for non-negativity.
- Ownership is on the row: every customer-scoped table carries customer_id, and child
  tables reference their parent through a COMPOSITE foreign key (parent_id, customer_id),
  so a child row cannot name a different customer than its parent.
- btree_gist exists solely so the policy-validity EXCLUDE constraint can combine an
  equality on policy_type with a range overlap on validity.
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

UPGRADE_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE customers (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email         text NOT NULL UNIQUE,
    display_name  text NOT NULL,
    password_hash text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE auth_sessions (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id uuid NOT NULL REFERENCES customers (id),
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    revoked_at  timestamptz,
    CONSTRAINT auth_sessions_expiry_after_creation CHECK (expires_at > created_at)
);
CREATE INDEX auth_sessions_customer_idx ON auth_sessions (customer_id);

CREATE TABLE products (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    sku              text NOT NULL UNIQUE,
    name             text NOT NULL,
    price_cents      bigint NOT NULL CONSTRAINT products_price_nonneg CHECK (price_cents >= 0),
    is_digital_addon boolean NOT NULL DEFAULT false
);

CREATE TABLE subscriptions (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id          uuid NOT NULL REFERENCES customers (id),
    plan_type            text NOT NULL CHECK (plan_type IN ('monthly', 'annual')),
    status               text NOT NULL DEFAULT 'active'
                         CHECK (status IN ('active', 'cancel_scheduled', 'canceled')),
    started_at           timestamptz NOT NULL,
    first_charge_at      timestamptz NOT NULL,
    first_charge_cents   bigint NOT NULL
                         CONSTRAINT subscriptions_first_charge_nonneg CHECK (first_charge_cents >= 0),
    price_cents          bigint NOT NULL
                         CONSTRAINT subscriptions_price_nonneg CHECK (price_cents >= 0),
    current_period_start timestamptz NOT NULL,
    current_period_end   timestamptz NOT NULL,
    canceled_at          timestamptz,
    cancel_effective_at  timestamptz,
    CONSTRAINT subscriptions_period_order CHECK (current_period_end > current_period_start),
    CONSTRAINT subscriptions_cancel_consistent CHECK (
        (status = 'active' AND canceled_at IS NULL AND cancel_effective_at IS NULL)
        OR (status <> 'active' AND canceled_at IS NOT NULL AND cancel_effective_at IS NOT NULL)
    ),
    CONSTRAINT subscriptions_id_customer_uq UNIQUE (id, customer_id)
);
CREATE INDEX subscriptions_customer_idx ON subscriptions (customer_id);

CREATE TABLE orders (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    order_number            text NOT NULL UNIQUE,
    customer_id             uuid NOT NULL REFERENCES customers (id),
    kind                    text NOT NULL DEFAULT 'purchase'
                            CHECK (kind IN ('purchase', 'subscription_charge')),
    subscription_id         uuid,
    replacement_for_order_id uuid,
    placed_at               timestamptz NOT NULL,
    -- The purchase date (selects the policy version) is the UTC calendar date of placed_at.
    purchase_date           date GENERATED ALWAYS AS ((placed_at AT TIME ZONE 'UTC')::date) STORED,
    status                  text NOT NULL DEFAULT 'placed'
                            CHECK (status IN ('placed', 'shipped', 'delivered', 'canceled')),
    list_total_cents        bigint NOT NULL CONSTRAINT orders_list_nonneg CHECK (list_total_cents >= 0),
    discount_cents          bigint NOT NULL DEFAULT 0
                            CONSTRAINT orders_discount_nonneg CHECK (discount_cents >= 0),
    shipping_cents          bigint NOT NULL DEFAULT 0
                            CONSTRAINT orders_shipping_nonneg CHECK (shipping_cents >= 0),
    paid_cents              bigint NOT NULL CONSTRAINT orders_paid_nonneg CHECK (paid_cents >= 0),
    -- Denormalized running total of refunds. Maintained ONLY by the refunds INSERT trigger
    -- (migration 0002). The CHECK below is the paid-amount invariant: it is a system
    -- invariant independent of any policy text.
    refunded_total_cents    bigint NOT NULL DEFAULT 0
                            CONSTRAINT orders_refunded_nonneg CHECK (refunded_total_cents >= 0),
    CONSTRAINT orders_discount_le_list CHECK (discount_cents <= list_total_cents),
    CONSTRAINT orders_paid_consistent
        CHECK (paid_cents = list_total_cents - discount_cents + shipping_cents),
    CONSTRAINT orders_refund_within_paid CHECK (refunded_total_cents <= paid_cents),
    CONSTRAINT orders_id_customer_uq UNIQUE (id, customer_id),
    CONSTRAINT orders_subscription_owner_fk FOREIGN KEY (subscription_id, customer_id)
        REFERENCES subscriptions (id, customer_id),
    CONSTRAINT orders_replacement_owner_fk FOREIGN KEY (replacement_for_order_id, customer_id)
        REFERENCES orders (id, customer_id)
);
CREATE INDEX orders_customer_placed_idx ON orders (customer_id, placed_at DESC);

CREATE TABLE order_items (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id             uuid NOT NULL,
    customer_id          uuid NOT NULL,
    product_id           uuid NOT NULL REFERENCES products (id),
    qty                  integer NOT NULL CHECK (qty > 0),
    unit_price_cents     bigint NOT NULL CONSTRAINT order_items_unit_nonneg CHECK (unit_price_cents >= 0),
    -- Amount actually paid for this line, after discount allocation (never list price).
    paid_cents           bigint NOT NULL CONSTRAINT order_items_paid_nonneg CHECK (paid_cents >= 0),
    final_sale           boolean NOT NULL DEFAULT false,
    digital_accessed_at  timestamptz,
    CONSTRAINT order_items_owner_fk FOREIGN KEY (order_id, customer_id)
        REFERENCES orders (id, customer_id)
);
CREATE INDEX order_items_order_idx ON order_items (order_id);

CREATE TABLE shipments (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id    uuid NOT NULL,
    customer_id uuid NOT NULL,
    carrier     text NOT NULL,
    tracking_no text NOT NULL,
    status      text NOT NULL
                CHECK (status IN ('label_created', 'in_transit', 'delivered', 'exception', 'lost')),
    shipped_at  timestamptz,
    delivered_at timestamptz,
    expected_at timestamptz,
    CONSTRAINT shipments_delivered_consistent
        CHECK ((status = 'delivered') = (delivered_at IS NOT NULL)),
    CONSTRAINT shipments_id_customer_uq UNIQUE (id, customer_id),
    CONSTRAINT shipments_owner_fk FOREIGN KEY (order_id, customer_id)
        REFERENCES orders (id, customer_id)
);
CREATE INDEX shipments_order_idx ON shipments (order_id);

CREATE TABLE shipment_events (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    shipment_id uuid NOT NULL,
    customer_id uuid NOT NULL,
    occurred_at timestamptz NOT NULL,
    code        text NOT NULL,
    detail      text,
    CONSTRAINT shipment_events_owner_fk FOREIGN KEY (shipment_id, customer_id)
        REFERENCES shipments (id, customer_id)
);
CREATE INDEX shipment_events_shipment_idx ON shipment_events (shipment_id, occurred_at);

-- Policies. Versions of one policy type can never overlap in time.
CREATE TABLE policy_documents (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    policy_type  text NOT NULL CHECK (policy_type IN ('shipping', 'refund', 'cancellation')),
    version      integer NOT NULL CHECK (version > 0),
    validity     daterange NOT NULL,
    title        text NOT NULL,
    source_path  text NOT NULL,
    content_hash text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT policy_documents_validity_sane
        CHECK (NOT isempty(validity) AND NOT lower_inf(validity) AND lower_inc(validity)),
    CONSTRAINT policy_documents_type_version_uq UNIQUE (policy_type, version),
    CONSTRAINT policy_documents_no_overlap
        EXCLUDE USING gist (policy_type WITH =, validity WITH &&)
);

CREATE TABLE policy_chunks (
    id                        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id               uuid NOT NULL REFERENCES policy_documents (id),
    ordinal                   integer NOT NULL CHECK (ordinal >= 0),
    text                      text NOT NULL,
    tsv                       tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    -- 384 = output dimension of bge-small-en-v1.5. A model change needs a migration.
    embedding                 vector(384) NOT NULL,
    embedding_model           text NOT NULL CHECK (embedding_model <> ''),
    embedding_model_revision  text NOT NULL CHECK (embedding_model_revision <> ''),
    -- Prompt Guard verdict, a detection annotation only. Policy text is scanned once at ingestion.
    injection_verdict         text NOT NULL DEFAULT 'unscanned'
                              CHECK (injection_verdict IN ('unscanned', 'clean', 'flagged')),
    injection_scanned_at      timestamptz,
    CONSTRAINT policy_chunks_doc_ordinal_uq UNIQUE (document_id, ordinal)
);
CREATE INDEX policy_chunks_tsv_idx ON policy_chunks USING gin (tsv);
CREATE INDEX policy_chunks_embedding_idx ON policy_chunks
    USING hnsw (embedding vector_cosine_ops);
"""

DOWNGRADE_SQL = """
DROP TABLE policy_chunks, policy_documents, shipment_events, shipments, order_items,
           orders, subscriptions, products, auth_sessions, customers;
DROP EXTENSION IF EXISTS btree_gist;
DROP EXTENSION IF EXISTS vector;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(UPGRADE_SQL)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(DOWNGRADE_SQL)
