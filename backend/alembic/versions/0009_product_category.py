"""Product category: a structured fact from live order data, used to scope retrieval.

Revision ID: 0009
Revises: 0008

Retrieval's candidate set is narrowed by structured facts from live order and subscription data
(for example: the gift card policy is in scope only when the order contains a gift card). The
category is the fact. It is reference data, set by the catalogue, never by a model or a customer.
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

CATEGORIES = (
    "'general', 'digital_addon', 'gift_card', 'plan', 'hazardous', 'perishable', 'oversize',"
    " 'age_restricted'"
)


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        "ALTER TABLE products ADD COLUMN category text NOT NULL DEFAULT 'general'"
        f" CONSTRAINT products_category_check CHECK (category IN ({CATEGORIES}))"  # noqa: S608
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql("ALTER TABLE products DROP COLUMN category")
