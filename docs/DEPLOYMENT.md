# Deployment notes (SIMULATED-DATA project)

Target: Oracle Cloud Infrastructure Always Free, Ampere A1 (arm64). Fallback if capacity is unavailable after two hours of effort: a recorded demo plus the documented `docker compose` quickstart, with the capacity issue noted in the README.

## Database roles and row-level security (required before ANY deployment)

Customer isolation is enforced by the database (SPEC 17), and it fails **silently**: a connection as the superuser or as a table owner bypasses row-level security with no error. Getting the roles wrong on OCI gives a working application with no isolation. Follow this in order.

| Role | Owns / may do | Used by | URL variable |
|---|---|---|---|
| bootstrap superuser | creates roles and databases; seeds data | an operator, once; never a service | `ADMIN_DATABASE_URL` |
| `support_owner` | owns the database, tables and views; runs migrations; `NOSUPERUSER NOBYPASSRLS` | Alembic (the compose `migrate` service), policy ingestion | `MIGRATION_DATABASE_URL` |
| `support_app` | owns nothing; only the DML granted by the migrations; `NOSUPERUSER NOBYPASSRLS` | the backend | `DATABASE_URL` |

### Provisioning (any database, idempotent)

1. As the bootstrap superuser, with the two role passwords from your secrets store (never committed, no default):

   ```
   export SUPPORT_OWNER_PASSWORD=...  SUPPORT_APP_PASSWORD=...
   export ADMIN_DATABASE_URL=postgresql+psycopg://<superuser>:<pw>@<host>:5432/<db>
   python -m app.db.bootstrap
   ```

   This runs `backend/app/db/roles.sql`: creates `support_owner` and `support_app`, makes `support_owner` the database owner, revokes the default PUBLIC access, and transfers any existing objects to `support_owner`. Re-running it is safe and resets the two passwords. The `vector` and `btree_gist` extensions need a superuser: create them in the database first (`CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS btree_gist;`). On a **fresh docker compose volume** this happens automatically (`db/init/02-roles.sh`); an **existing** volume does not re-run init scripts, so run the command above once against it.
2. Run the migrations **as `support_owner`**: `MIGRATION_DATABASE_URL=postgresql+psycopg://support_owner:<pw>@<host>:5432/<db> alembic upgrade head` (compose: the `migrate` service). Migration 0012 creates the policies and the application role's grants, and refuses to run if `support_app` does not exist.
3. Seed demo data **as the bootstrap superuser** (`ADMIN_DATABASE_URL`, `python -m app.seed`): seeding writes every customer's rows, which row-level security forbids to every other role.
4. Point the backend `DATABASE_URL` at `support_app`. Keep the owner's and the superuser's credentials out of the backend's environment entirely.

### How the current customer reaches the database

The backend sets `app.current_customer_id` with `set_config(..., true)` (that is `SET LOCAL`) inside the one transaction that wraps each request, from the authenticated session only. It is **never** set with `SET` or `is_local=false`: session-scoped state survives on a pooled connection and would carry one customer's identity into the next request. The value is read back in the next statement, so a connection in autocommit mode raises instead of silently not applying. No customer set means no rows.

### Startup check and verification on the target

The backend refuses to start if its role is a superuser, has BYPASSRLS or owns a table (`PrivilegedRoleError` at startup), so a wrong `DATABASE_URL` is loud. Verify on the instance, as `support_app`:

```
SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user;   -- f | f
SELECT count(*) FROM orders;                                                -- 0 (no customer set)
SELECT relname FROM pg_class WHERE relrowsecurity AND NOT relforcerowsecurity AND relkind = 'r';  -- no rows
```

After every migration run `python -m app.db.rls_guard` (CI does, against a scratch database migrated from empty): it fails with the table's name if any table has row-level security enabled but not forced, a `customer_id` table with none, or a table that is neither protected nor listed as exempt. Migration 0014 lifts FORCE for a backfill and restores it; this is the check that proves it did, and that proves it for every migration after it.

Rules to remember: a **data migration** that must see every customer's rows cannot run as `support_owner` (FORCE row-level security applies to the owner too); run it as the bootstrap superuser. Views over protected tables must be `security_invoker` (the migration sets this for the refund drift views), or they run as the owner and silently return nothing. Passwords come from environment variables or a secrets store; never commit them.

## arm64

All images must build and run on `linux/arm64`. The real arm64 verification is a native build on the OCI instance at M4; manifest and wheel evidence is tracked in `docs/SPEC.md` section 9.

## Status

The roles, policies, grants, bootstrap script and compose wiring are built and tested locally. Nothing has been run on OCI yet.

**Compose, clean volume, run end to end (2026-11-12, a throwaway project, `docker compose -p srverify up --build`).** Verified: `db/init/02-roles.sh` created both roles (neither superuser nor BYPASSRLS); the `migrate` service ran Alembic as `support_owner` to 0013 and exited 0 (all 27 tables owned by `support_owner`); the backend started and held its connections as `support_app` only; seeding (bootstrap superuser) and policy ingestion (owner) ran from the backend image; a request without a session was a 401; a request with a session resolved the customer under RLS, ran the workflow, wrote its record (messages, turn trace, tagged `llm_calls` row) as `support_app`, and answered honestly with state `FAILED` when the model call could not be served; and the backend **refused to start** with `DATABASE_URL` set to the owner ("owns 27 table(s)") and to the superuser ("is a superuser and has BYPASSRLS").

**What did not work out of the box, and is not fixed here:**
1. The compose defaults cannot serve `/chat`: `LLM_PROVIDER=fake` needs `LLM_FIXTURES_DIR` and compose sets none, so the route returns 503 "assistant not configured". With fixtures mounted it still cannot succeed, because the FakeAdapter is keyed by scenario ID and `/chat` carries none (`FixtureMissing`, reported honestly as `FAILED`). A successful reply in compose needs a real provider: `GROQ_API_KEY` is not passed into the backend service's environment by `docker-compose.yml`. A decision for the PM, not made here.
2. There is no login route: a session exists only through `create_session` (tests) or SQL. The verification inserted one row as the superuser.
3. Seeding and policy ingestion are not compose services; they were run with `docker compose run -e ADMIN_DATABASE_URL=... backend python -m app.seed` and the same for `app.retrieval.ingest` with `MIGRATION_DATABASE_URL`. A one-command first start does not exist yet.
4. Not verified: the 0013 downgrade, and a real-provider reply in the container.
