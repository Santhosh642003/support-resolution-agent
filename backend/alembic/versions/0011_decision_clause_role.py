"""Decisive versus supporting applied clauses.

Revision ID: 0011
Revises: 0010

A Decision's applied clauses are of two kinds (SPEC 10.5). DECISIVE: determined the outcome (had it
been absent or different the outcome would have changed). SUPPORTING: applied, but did not drive
the result. The explanation must cite every decisive clause; omitting a supporting one is allowed.
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        "ALTER TABLE decision_clauses ADD COLUMN role text NOT NULL DEFAULT 'supporting'"
        " CONSTRAINT decision_clauses_role_check CHECK (role IN ('decisive', 'supporting'))"
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql("ALTER TABLE decision_clauses DROP COLUMN role")
