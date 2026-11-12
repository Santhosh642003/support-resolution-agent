#!/bin/sh
# Runs once on first container start (empty data volume), after 01-extensions.sql, as the bootstrap
# superuser. Creates support_owner and support_app and hands this database to them (SPEC 17).
# The script itself is backend/app/db/roles.sql, mounted at /opt/roles.sql. An EXISTING volume does
# not re-run init scripts: bootstrap it with `python -m app.db.bootstrap` (docs/DEPLOYMENT.md).
set -e

: "${SUPPORT_OWNER_PASSWORD:?SUPPORT_OWNER_PASSWORD must be set}"
: "${SUPPORT_APP_PASSWORD:?SUPPORT_APP_PASSWORD must be set}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -v owner_pw="$SUPPORT_OWNER_PASSWORD" -v app_pw="$SUPPORT_APP_PASSWORD" <<'SQL'
SELECT set_config('bootstrap.owner_password', :'owner_pw', false);
SELECT set_config('bootstrap.app_password', :'app_pw', false);
\i /opt/roles.sql
SQL
