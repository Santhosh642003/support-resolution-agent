"""Real-PostgreSQL fixtures. Each session gets a throwaway database migrated with Alembic.

These tests need the compose Postgres (`docker compose up -d db`) or a CI Postgres service.
If the server is unreachable they FAIL loudly; they never skip, so a green run cannot hide
an untested database.
"""

import uuid
from collections.abc import Iterator

import pytest
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import make_url

from alembic import command
from app.config import get_settings


def _alembic_config(url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    base = make_url(get_settings().database_url)
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
    test_url = base.set(database=name).render_as_string(hide_password=False)
    try:
        command.upgrade(_alembic_config(test_url), "head")
        eng = create_engine(test_url)
        yield eng
        eng.dispose()
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def conn(engine: Engine) -> Iterator[Connection]:
    """A connection inside a transaction that is always rolled back."""
    with engine.connect() as c:
        trans = c.begin()
        try:
            yield c
        finally:
            trans.rollback()
