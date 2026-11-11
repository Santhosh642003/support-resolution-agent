"""uv run python -m app.seed   (idempotent; safe to run repeatedly)"""

from sqlalchemy import create_engine

from app.config import get_settings
from app.seed.loader import seed_all


def main() -> None:
    engine = create_engine(get_settings().database_url)
    with engine.begin() as conn:  # one transaction: all or nothing
        report = seed_all(conn)
    print(report)


if __name__ == "__main__":
    main()
