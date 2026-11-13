"""A check that runs after migrations: row-level security is enabled AND forced on every table.

    ADMIN_DATABASE_URL=... python -m app.db.rls_guard    # exit status 1 and a list on any problem

Why it exists (SPEC 17, migration 0014): a migration may have to lift FORCE ROW LEVEL SECURITY to
backfill data as the table owner. A migration that turns a security control off must provably turn
it back on, and so must every future one. This does not read what a migration says it does; it reads
the catalog afterwards, so it fails whatever the cause: a forgotten re-FORCE, a table made without
policies, a dropped policy, an exemption that is no longer true.

A table is a problem if any of:
  * RLS is enabled but not forced (the owner then bypasses it silently), or has no policy;
  * it has a `customer_id` column and RLS is not enabled;
  * it is not protected and is not in EXEMPT with a stated reason (or is in EXEMPT and protected).
"""

import sys

from sqlalchemy import Connection, create_engine, text

# Tables DELIBERATELY not row-level protected, with the reason. A table that is neither protected
# nor listed here is a problem: protection (or a stated exemption) is required of every table.
EXEMPT: dict[str, str] = {
    "alembic_version": "migration bookkeeping, no customer data",
    "products": "shared reference data, read-only to the app role",
    "policy_documents": "shared reference data, read-only to the app role",
    "policy_chunks": "shared reference data, read-only to the app role",
    "policy_incorporations": "shared reference data, read-only to the app role",
    # Columns are (provider text, day date, requests integer): counters, no text of any kind, so it
    # cannot hold customer data. (llm_cache and llm_calls CAN, and are protected: migration 0013.)
    "llm_usage_daily": "global provider quota counters; no text column, cannot hold customer data",
}

_CATALOG_SQL = (
    "SELECT c.relname, c.relrowsecurity AS rls, c.relforcerowsecurity AS forced,"
    " EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid"
    "   AND a.attname = 'customer_id' AND NOT a.attisdropped) AS has_customer,"
    " (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) AS policies"
    " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
    " WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') ORDER BY 1"
)


def find_rls_problems(conn: Connection) -> list[str]:
    """Every violation, as a readable line; empty when the catalog is as required."""
    problems: list[str] = []
    protected: set[str] = set()
    for r in conn.execute(text(_CATALOG_SQL)):
        if r.rls:
            protected.add(r.relname)
            if not r.forced:
                problems.append(f"{r.relname}: row-level security is enabled but NOT FORCED")
            if r.policies < 1:
                problems.append(f"{r.relname}: row-level security is enabled but has no policy")
        elif r.has_customer:
            problems.append(f"{r.relname}: has customer_id but row-level security is not enabled")
        elif r.relname not in EXEMPT:
            problems.append(f"{r.relname}: neither protected nor listed as exempt")
    for name in sorted(set(EXEMPT) & protected):
        problems.append(f"{name}: listed as exempt but is protected; remove the stale exemption")
    return problems


def main() -> None:
    from app.config import get_settings

    settings = get_settings()
    url = settings.admin_database_url or settings.migration_database_url or settings.database_url
    engine = create_engine(url)
    with engine.connect() as conn:
        problems = find_rls_problems(conn)
    if problems:
        print("ROW-LEVEL SECURITY CHECK FAILED:", *problems, sep="\n  ")
        sys.exit(1)
    print(
        "row-level security check passed: every protected table is enabled, forced and has a policy"
    )


if __name__ == "__main__":
    main()
