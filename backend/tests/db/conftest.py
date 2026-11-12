"""Real-PostgreSQL fixtures. Each session gets a throwaway database migrated with Alembic.

These tests need the compose Postgres (`docker compose up -d db`) or a CI Postgres service.
If the server is unreachable they FAIL loudly; they never skip, so a green run cannot hide
an untested database.
"""

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import URL, make_url

from alembic import command
from app.config import get_settings
from app.db.bootstrap import bootstrap


def _alembic_config(url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


OWNER_PASSWORD = os.environ.get("SUPPORT_OWNER_PASSWORD", "support_owner")  # dev defaults (compose)
APP_PASSWORD = os.environ.get("SUPPORT_APP_PASSWORD", "support_app")


def admin_base_url() -> URL:
    """The bootstrap SUPERUSER (ADMIN_DATABASE_URL, else DATABASE_URL as the legacy single role)."""
    settings = get_settings()
    return make_url(settings.admin_database_url or settings.database_url)


@contextmanager
def make_database() -> Iterator[tuple[str, Engine]]:
    """A throwaway database, migrated as support_owner exactly as a deployment is (SPEC 17):
    created by the bootstrap superuser, handed to support_owner by the roles script, migrated by
    support_owner. Yields (name, superuser engine on it)."""
    base = admin_base_url()
    name = f"support_test_{uuid.uuid4().hex[:10]}"
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{name}" STRATEGY = WAL_LOG'))
    except Exception as exc:  # noqa: BLE001
        pytest.fail(
            f"PostgreSQL unreachable at {base.render_as_string(hide_password=True)}: {exc}. "
            "Start it with `docker compose up -d db`."
        )
    try:
        superuser = create_engine(base.set(database=name))
        with superuser.begin() as c:  # extensions need a superuser; the owner role has none
            c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            c.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
        bootstrap(superuser, OWNER_PASSWORD, APP_PASSWORD)
        command.upgrade(
            _alembic_config(_role_url(base, name, "support_owner", OWNER_PASSWORD)), "head"
        )
        yield name, superuser
        superuser.dispose()
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="session")
def test_database() -> Iterator[tuple[str, Engine]]:
    with make_database() as db:
        yield db


def _role_url(base: URL, database: str, role: str, password: str) -> str:
    return base.set(database=database, username=role, password=password).render_as_string(
        hide_password=False
    )


@pytest.fixture(scope="session")
def engine(test_database: tuple[str, Engine]) -> Engine:
    """The bootstrap SUPERUSER on the test database. It bypasses row-level security, so it is for
    setup and for tests of things that are not RLS. Every RLS test uses `app_engine` instead."""
    return test_database[1]


@pytest.fixture(scope="session")
def app_engine(test_database: tuple[str, Engine]) -> Iterator[Engine]:
    """support_app: what the application connects as (not a superuser, owns nothing, RLS applies)."""
    eng = create_engine(_role_url(admin_base_url(), test_database[0], "support_app", APP_PASSWORD))
    yield eng
    eng.dispose()


@pytest.fixture(scope="session")
def owner_engine(test_database: tuple[str, Engine]) -> Iterator[Engine]:
    """support_owner: owns the tables and runs migrations. FORCE RLS applies to it too."""
    eng = create_engine(
        _role_url(admin_base_url(), test_database[0], "support_owner", OWNER_PASSWORD)
    )
    yield eng
    eng.dispose()


@pytest.fixture
def conn(engine: Engine) -> Iterator[Connection]:
    """A connection inside a transaction that is always rolled back."""
    with engine.connect() as c:
        trans = c.begin()
        try:
            yield c
        finally:
            trans.rollback()
