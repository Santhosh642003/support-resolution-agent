"""Diagnostics on llm_calls: the redacted provider error body, and native-JSON fallbacks.

Revision ID: 0007
Revises: 0006

- error_detail: the provider's error body (REDACTED of keys and organisation ids, truncated), so
  an unexplained failure is diagnosable the first time it happens.
- native_fallbacks: how many times the provider rejected the native JSON hint (HTTP 400) and the
  call was retried once without it. Reported in evaluation: zero is a measurement, a high count
  is a finding.
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        "ALTER TABLE llm_calls"
        " ADD COLUMN error_detail text,"
        " ADD COLUMN native_fallbacks integer NOT NULL DEFAULT 0"
        " CONSTRAINT llm_calls_fallbacks_nonneg CHECK (native_fallbacks >= 0)"
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        "ALTER TABLE llm_calls DROP COLUMN native_fallbacks, DROP COLUMN error_detail"
    )
