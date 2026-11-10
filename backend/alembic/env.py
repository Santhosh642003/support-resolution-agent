"""Alembic environment. Migrations are hand-written SQL (no autogenerate), so the
database, not the ORM, is the source of truth for constraints."""

from sqlalchemy import create_engine, pool

from alembic import context
from app.config import get_settings

config = context.config
url = config.get_main_option("sqlalchemy.url") or get_settings().database_url

if context.is_offline_mode():
    context.configure(url=url, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
