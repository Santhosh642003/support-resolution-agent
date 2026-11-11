"""Policy structure: more policy types, audience/status, and first-class incorporation links.

Revision ID: 0008
Revises: 0007

- More policy types (merchant_returns, gift_cards, shipping_restrictions, warranty, faq_archive).
- policy_documents.audience ('customer' | 'merchant') and .status ('current' | 'archived').
  A seller-facing policy does not apply to customers; an archived document is superseded. Both
  are immutable once published, like the rest of a document's identity.
- policy_incorporations: a clause of a LATER version declares that it incorporates specific
  clauses of an EARLIER version of the same policy ("as v1, but ...", "unchanged"). The
  date-validity range still decides which version is IN EFFECT; an incorporated clause is
  retrieved only because the in-effect clause pulled it in, never because its own validity matched.
  The link is capped at ONE hop: a chain is rejected, because needing one is a finding to report,
  not a feature to build. Links are append-only.
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

TYPES = (
    "'shipping', 'refund', 'cancellation', 'merchant_returns', 'gift_cards',"
    " 'shipping_restrictions', 'warranty', 'faq_archive'"
)

UPGRADE_SQL = f"""
ALTER TABLE policy_documents DROP CONSTRAINT policy_documents_policy_type_check;
ALTER TABLE policy_documents ADD CONSTRAINT policy_documents_policy_type_check
    CHECK (policy_type IN ({TYPES}));

ALTER TABLE policy_documents
    ADD COLUMN audience text NOT NULL DEFAULT 'customer'
        CONSTRAINT policy_documents_audience_check CHECK (audience IN ('customer', 'merchant')),
    ADD COLUMN status text NOT NULL DEFAULT 'current'
        CONSTRAINT policy_documents_status_check CHECK (status IN ('current', 'archived'));

CREATE OR REPLACE FUNCTION policy_documents_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF NEW.policy_type IS DISTINCT FROM OLD.policy_type
       OR NEW.version IS DISTINCT FROM OLD.version
       OR NEW.validity IS DISTINCT FROM OLD.validity
       OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
       OR NEW.title IS DISTINCT FROM OLD.title
       OR NEW.audience IS DISTINCT FROM OLD.audience
       OR NEW.status IS DISTINCT FROM OLD.status THEN
        RAISE EXCEPTION 'policy document % is immutable; publish a new version instead', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END
$fn$;

CREATE TABLE policy_incorporations (
    source_chunk_key text NOT NULL REFERENCES policy_chunks (chunk_key),
    target_chunk_key text NOT NULL REFERENCES policy_chunks (chunk_key),
    PRIMARY KEY (source_chunk_key, target_chunk_key),
    CONSTRAINT policy_incorporations_not_self CHECK (source_chunk_key <> target_chunk_key)
);
CREATE INDEX policy_incorporations_target_idx ON policy_incorporations (target_chunk_key);

CREATE FUNCTION policy_incorporations_check() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    s policy_chunks%ROWTYPE;
    t policy_chunks%ROWTYPE;
BEGIN
    SELECT * INTO s FROM policy_chunks WHERE chunk_key = NEW.source_chunk_key;
    SELECT * INTO t FROM policy_chunks WHERE chunk_key = NEW.target_chunk_key;
    IF s.policy_type <> t.policy_type THEN
        RAISE EXCEPTION 'a clause may only incorporate a clause of the same policy (% -> %)',
            s.chunk_key, t.chunk_key USING ERRCODE = 'check_violation';
    END IF;
    IF t.version >= s.version THEN
        RAISE EXCEPTION 'a clause may only incorporate a clause of an EARLIER version (% -> %)',
            s.chunk_key, t.chunk_key USING ERRCODE = 'check_violation';
    END IF;
    IF EXISTS (SELECT 1 FROM policy_incorporations WHERE source_chunk_key = NEW.target_chunk_key)
       OR EXISTS (SELECT 1 FROM policy_incorporations WHERE target_chunk_key = NEW.source_chunk_key)
    THEN
        RAISE EXCEPTION 'incorporation chains are not supported (one hop only): % -> %',
            s.chunk_key, t.chunk_key USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER policy_incorporations_check_trg BEFORE INSERT ON policy_incorporations
    FOR EACH ROW EXECUTE FUNCTION policy_incorporations_check();
CREATE TRIGGER policy_incorporations_immutable BEFORE UPDATE OR DELETE ON policy_incorporations
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER policy_incorporations_no_truncate BEFORE TRUNCATE ON policy_incorporations
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();
"""  # noqa: S608 (constant type list, no input)

DOWNGRADE_SQL = """
CREATE OR REPLACE FUNCTION policy_documents_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF NEW.policy_type IS DISTINCT FROM OLD.policy_type
       OR NEW.version IS DISTINCT FROM OLD.version
       OR NEW.validity IS DISTINCT FROM OLD.validity
       OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
       OR NEW.title IS DISTINCT FROM OLD.title THEN
        RAISE EXCEPTION 'policy document % is immutable; publish a new version instead', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END
$fn$;
DROP TABLE policy_incorporations;
DROP FUNCTION policy_incorporations_check();
ALTER TABLE policy_documents DROP COLUMN status, DROP COLUMN audience;
ALTER TABLE policy_documents DROP CONSTRAINT policy_documents_policy_type_check;
ALTER TABLE policy_documents ADD CONSTRAINT policy_documents_policy_type_check
    CHECK (policy_type IN ('shipping', 'refund', 'cancellation'));
"""


def upgrade() -> None:
    # psycopg treats % as a placeholder marker; double literal percent signs.
    op.get_bind().exec_driver_sql(UPGRADE_SQL.replace("%", "%%"))


def downgrade() -> None:
    op.get_bind().exec_driver_sql(DOWNGRADE_SQL.replace("%", "%%"))
