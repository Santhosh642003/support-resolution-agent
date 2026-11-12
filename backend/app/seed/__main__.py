"""uv run python -m app.seed   (idempotent; safe to run repeatedly)"""

from sqlalchemy import create_engine

from app.config import get_settings
from app.seed.loader import seed_all


def main() -> None:
    settings = get_settings()
    # Seeding writes every customer's rows, which row-level security forbids to every role without
    # BYPASSRLS: it is a bootstrap operation, run as the bootstrap superuser (SPEC 17), never as
    # support_app or support_owner.
    url = settings.admin_database_url
    if not url:
        raise SystemExit("ADMIN_DATABASE_URL must be set to seed (the bootstrap superuser)")
    engine = create_engine(url)
    with engine.begin() as conn:  # one transaction: all or nothing
        report = seed_all(conn)
    print(report)


if __name__ == "__main__":
    main()
