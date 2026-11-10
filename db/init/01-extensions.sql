-- Runs once on first container start (empty data volume). Alembic migrations
-- also use IF NOT EXISTS so this is idempotent. Simulated-data project.
CREATE EXTENSION IF NOT EXISTS vector;
