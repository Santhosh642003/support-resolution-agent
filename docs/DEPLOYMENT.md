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

Rules to remember: a **data migration** that must see every customer's rows cannot run as `support_owner` (FORCE row-level security applies to the owner too); run it as the bootstrap superuser. Views over protected tables must be `security_invoker` (the migration sets this for the refund drift views), or they run as the owner and silently return nothing. Passwords come from environment variables or a secrets store; never commit them.

## arm64

All images must build and run on `linux/arm64`. The real arm64 verification is a native build on the OCI instance at M4; manifest and wheel evidence is tracked in `docs/SPEC.md` section 9.

## Status

The roles, policies, grants, bootstrap script and compose wiring are built and tested locally (fresh-volume path on a throwaway container; the existing development database converted). Nothing has been run on OCI yet.
