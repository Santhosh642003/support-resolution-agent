# Deployment notes (SIMULATED-DATA project)

Target: Oracle Cloud Infrastructure Always Free, Ampere A1 (arm64). Fallback if capacity is unavailable after two hours of effort: a recorded demo plus the documented `docker compose` quickstart, with the capacity issue noted in the README.

## Database roles (required before any non-local deployment)

The application must NOT connect as the PostgreSQL superuser or as the table owner, because both bypass row-level security and database triggers can be disabled by the owner. See `docs/SPEC.md` section 17.

1. Using the bootstrap superuser, create `support_owner` (owns the schema, runs migrations) and `support_app` (runtime role; `NOSUPERUSER NOBYPASSRLS`; not an owner; limited DML grants). Script: `db/roles.sql` (to be written in M2 task 0).
2. Run `alembic upgrade head` as `support_owner`.
3. Point the backend `DATABASE_URL` at `support_app`. Keep the owner and superuser credentials out of the backend environment.
4. Passwords are supplied through environment variables or a secrets store. Never commit them.

## arm64

All images must build and run on `linux/arm64`. The real arm64 verification is a native build on the OCI instance at M4; manifest and wheel evidence is tracked in `docs/SPEC.md` section 9.

## Status

Roles, RLS policies and this checklist are planned for M2 task 0; nothing in this file has been executed yet.
