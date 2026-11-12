-- Database roles (SPEC 17). IDEMPOTENT. Run as the bootstrap SUPERUSER, connected to the target
-- database, by `python -m app.db.bootstrap` (or psql: see docs/DEPLOYMENT.md).
--
--   support_owner : owns the database and every table; runs Alembic. LOGIN, NOSUPERUSER, NOBYPASSRLS.
--   support_app   : the application at runtime. LOGIN, NOSUPERUSER, NOBYPASSRLS, owns NOTHING; the
--                   per-table DML it holds is granted by the migrations (never here).
--
-- A superuser and a table owner both bypass row-level security, so the application must be neither.
-- Passwords are NOT in this file: the caller sets the session settings bootstrap.owner_password and
-- bootstrap.app_password first; an unset or empty one aborts the script.

DO $do$
DECLARE
    owner_pw text := current_setting('bootstrap.owner_password');
    app_pw   text := current_setting('bootstrap.app_password');
BEGIN
    IF coalesce(owner_pw, '') = '' OR coalesce(app_pw, '') = '' THEN
        RAISE EXCEPTION 'bootstrap.owner_password and bootstrap.app_password must be set';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'support_owner') THEN
        EXECUTE format('CREATE ROLE support_owner LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB '
                       'NOCREATEROLE NOREPLICATION PASSWORD %L', owner_pw);
    ELSE
        EXECUTE format('ALTER ROLE support_owner LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB '
                       'NOCREATEROLE NOREPLICATION PASSWORD %L', owner_pw);
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'support_app') THEN
        EXECUTE format('CREATE ROLE support_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB '
                       'NOCREATEROLE NOREPLICATION PASSWORD %L', app_pw);
    ELSE
        EXECUTE format('ALTER ROLE support_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB '
                       'NOCREATEROLE NOREPLICATION PASSWORD %L', app_pw);
    END IF;
END
$do$;

-- This database: owned by support_owner; nobody connects by default; the schema is not writable by
-- PUBLIC and the app may only use it.
DO $do$
BEGIN
    EXECUTE format('ALTER DATABASE %I OWNER TO support_owner', current_database());
    EXECUTE format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database());
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO support_owner, support_app', current_database());
END
$do$;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO support_app;
GRANT USAGE, CREATE ON SCHEMA public TO support_owner;

-- A database created before these roles existed (the local development volume) has objects owned by
-- the bootstrap superuser. Hand them to support_owner (a no-op on a database the owner created).
-- Extension members are skipped, and sequences owned by a column follow their table.
DO $do$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT c.oid::regclass AS obj,
               CASE c.relkind WHEN 'S' THEN 'SEQUENCE' WHEN 'v' THEN 'VIEW'
                              WHEN 'm' THEN 'MATERIALIZED VIEW' ELSE 'TABLE' END AS kind
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'S')
          AND pg_get_userbyid(c.relowner) <> 'support_owner'
          AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = c.oid AND d.deptype IN ('e', 'a'))
        ORDER BY (c.relkind = 'S')
    LOOP
        EXECUTE format('ALTER %s %s OWNER TO support_owner', r.kind, r.obj);
    END LOOP;
    FOR r IN
        SELECT p.oid::regprocedure AS obj
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public' AND pg_get_userbyid(p.proowner) <> 'support_owner'
          AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e')
    LOOP
        EXECUTE format('ALTER FUNCTION %s OWNER TO support_owner', r.obj);
    END LOOP;
END
$do$;
