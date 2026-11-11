"""Daily token counter beside the daily request counter.

Revision ID: 0006
Revises: 0005

The measured binding provider limit is tokens per day (200K on the free tier), not requests,
so the persisted counter tracks both. Tokens are reserved before a call (an estimate) and
settled to the provider-reported usage afterwards (app/providers/ratelimit.py).
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        "ALTER TABLE llm_usage_daily"
        " ADD COLUMN tokens bigint NOT NULL DEFAULT 0 CONSTRAINT llm_usage_daily_tokens_nonneg CHECK (tokens >= 0)"
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql("ALTER TABLE llm_usage_daily DROP COLUMN tokens")
