"""Create the database roles and hand an existing database to them (SPEC 17).

    SUPPORT_OWNER_PASSWORD=... SUPPORT_APP_PASSWORD=... \
        ADMIN_DATABASE_URL=postgresql+psycopg://<superuser>:...@host/db \
        python -m app.db.bootstrap

Runs `roles.sql` as the bootstrap SUPERUSER against the database named in the URL. Idempotent.
Passwords come from the environment, never from the repository; there is deliberately no default.
"""

import os
import sys
from pathlib import Path

from sqlalchemy import Engine, create_engine, text

ROLES_SQL = Path(__file__).with_name("roles.sql")
OWNER_ROLE = "support_owner"
APP_ROLE = "support_app"


def bootstrap(admin_engine: Engine, owner_password: str, app_password: str) -> None:
    script = ROLES_SQL.read_text(encoding="utf-8")
    with admin_engine.begin() as conn:
        for name, value in (
            ("bootstrap.owner_password", owner_password),
            ("bootstrap.app_password", app_password),
        ):
            conn.execute(text("SELECT set_config(:n, :v, true)"), {"n": name, "v": value})
        # The script contains format() placeholders (%L): run it on the raw driver connection with
        # no parameters so the driver does not try to interpret them (same session as set_config).
        conn.connection.driver_connection.execute(script)  # type: ignore[union-attr]


def main() -> None:
    from app.config import get_settings

    settings = get_settings()
    url = settings.admin_database_url
    owner_pw = os.environ.get("SUPPORT_OWNER_PASSWORD", "")
    app_pw = os.environ.get("SUPPORT_APP_PASSWORD", "")
    if not url or not owner_pw or not app_pw:
        sys.exit(
            "ADMIN_DATABASE_URL, SUPPORT_OWNER_PASSWORD and SUPPORT_APP_PASSWORD must all be set"
        )
    bootstrap(create_engine(url), owner_pw, app_pw)
    print("roles and ownership are in place; now run `alembic upgrade head` as support_owner")


if __name__ == "__main__":
    main()
