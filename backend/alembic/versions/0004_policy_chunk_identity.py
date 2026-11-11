"""Clause-level policy chunks with stable keys and denormalized validity.

Revision ID: 0004
Revises: 0003

- chunk_key is the human-meaningful citation identifier (e.g. refund-v2-clause-3). A CHECK
  forces it to equal policy_type-v<version>-clause-<clause_number>, so the scheme cannot drift.
- policy_type, version and validity are denormalized onto each chunk so retrieval can filter
  by date without a join. A composite FOREIGN KEY to policy_documents guarantees the copies
  always equal the document's values.
- Text and identity columns of documents and chunks are immutable once written (a changed
  policy needs a new version); embedding and Prompt Guard verdict columns remain updatable.
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

UPGRADE_SQL = """
ALTER TABLE policy_documents
    ADD CONSTRAINT policy_documents_identity_uq UNIQUE (id, policy_type, version, validity);

ALTER TABLE policy_chunks
    ADD COLUMN chunk_key     text,
    ADD COLUMN clause_number integer,
    ADD COLUMN policy_type   text,
    ADD COLUMN version       integer,
    ADD COLUMN validity      daterange;

ALTER TABLE policy_chunks
    ALTER COLUMN chunk_key SET NOT NULL,
    ALTER COLUMN clause_number SET NOT NULL,
    ALTER COLUMN policy_type SET NOT NULL,
    ALTER COLUMN version SET NOT NULL,
    ALTER COLUMN validity SET NOT NULL;

ALTER TABLE policy_chunks
    ADD CONSTRAINT policy_chunks_chunk_key_uq UNIQUE (chunk_key),
    ADD CONSTRAINT policy_chunks_clause_positive CHECK (clause_number > 0),
    ADD CONSTRAINT policy_chunks_key_scheme CHECK (
        chunk_key = policy_type || '-v' || version::text || '-clause-' || clause_number::text),
    ADD CONSTRAINT policy_chunks_doc_clause_uq UNIQUE (document_id, clause_number),
    ADD CONSTRAINT policy_chunks_document_identity_fk
        FOREIGN KEY (document_id, policy_type, version, validity)
        REFERENCES policy_documents (id, policy_type, version, validity);

CREATE INDEX policy_chunks_type_validity_idx ON policy_chunks USING gist (policy_type, validity);

CREATE FUNCTION policy_documents_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
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
CREATE TRIGGER policy_documents_guard_trg BEFORE UPDATE ON policy_documents
    FOR EACH ROW EXECUTE FUNCTION policy_documents_guard();

CREATE FUNCTION policy_chunks_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF NEW.chunk_key IS DISTINCT FROM OLD.chunk_key
       OR NEW.text IS DISTINCT FROM OLD.text
       OR NEW.clause_number IS DISTINCT FROM OLD.clause_number
       OR NEW.document_id IS DISTINCT FROM OLD.document_id
       OR NEW.policy_type IS DISTINCT FROM OLD.policy_type
       OR NEW.version IS DISTINCT FROM OLD.version
       OR NEW.validity IS DISTINCT FROM OLD.validity THEN
        RAISE EXCEPTION 'policy chunk % is immutable: a citation must keep its meaning', OLD.chunk_key
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER policy_chunks_guard_trg BEFORE UPDATE ON policy_chunks
    FOR EACH ROW EXECUTE FUNCTION policy_chunks_guard();
"""

DOWNGRADE_SQL = """
DROP TRIGGER policy_chunks_guard_trg ON policy_chunks;
DROP TRIGGER policy_documents_guard_trg ON policy_documents;
DROP FUNCTION policy_chunks_guard();
DROP FUNCTION policy_documents_guard();
DROP INDEX policy_chunks_type_validity_idx;
ALTER TABLE policy_chunks
    DROP CONSTRAINT policy_chunks_document_identity_fk,
    DROP CONSTRAINT policy_chunks_doc_clause_uq,
    DROP CONSTRAINT policy_chunks_key_scheme,
    DROP CONSTRAINT policy_chunks_clause_positive,
    DROP CONSTRAINT policy_chunks_chunk_key_uq,
    DROP COLUMN validity, DROP COLUMN version, DROP COLUMN policy_type,
    DROP COLUMN clause_number, DROP COLUMN chunk_key;
ALTER TABLE policy_documents DROP CONSTRAINT policy_documents_identity_uq;
"""


def upgrade() -> None:
    # psycopg treats % as a placeholder marker; double literal percent signs.
    op.get_bind().exec_driver_sql(UPGRADE_SQL.replace("%", "%%"))


def downgrade() -> None:
    op.get_bind().exec_driver_sql(DOWNGRADE_SQL)
