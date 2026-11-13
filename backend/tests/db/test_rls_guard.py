"""The post-migration row-level security check (app/db/rls_guard.py): it passes on a migrated
database and fails, naming the table, for every way protection can be left undone."""

from sqlalchemy import Connection, text

from app.db.rls_guard import EXEMPT, find_rls_problems


def test_a_freshly_migrated_database_has_no_problems(conn: Connection) -> None:
    assert find_rls_problems(conn) == []


def test_a_table_left_unforced_is_reported_whatever_the_cause(conn: Connection) -> None:
    """Exactly what a migration 0014 that forgot its last statement would leave behind."""
    for table in ("orders", "order_items", "llm_cache"):
        conn.execute(text(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"))  # noqa: S608
    problems = find_rls_problems(conn)
    assert sorted(p.split(":")[0] for p in problems) == ["llm_cache", "order_items", "orders"]
    assert all("NOT FORCED" in p for p in problems)


def test_a_new_table_with_a_customer_id_and_no_protection_is_reported(conn: Connection) -> None:
    conn.execute(text("CREATE TABLE zz_new (id int, customer_id uuid)"))
    assert find_rls_problems(conn) == [
        "zz_new: has customer_id but row-level security is not enabled"
    ]


def test_a_new_table_that_is_neither_protected_nor_exempt_is_reported(conn: Connection) -> None:
    conn.execute(text("CREATE TABLE zz_other (id int)"))
    assert find_rls_problems(conn) == ["zz_other: neither protected nor listed as exempt"]


def test_a_protected_table_whose_policy_was_dropped_is_reported(conn: Connection) -> None:
    conn.execute(text("DROP POLICY customer_isolation ON orders"))
    assert find_rls_problems(conn) == ["orders: row-level security is enabled but has no policy"]


def test_an_exemption_that_is_no_longer_true_is_reported(conn: Connection) -> None:
    conn.execute(text("ALTER TABLE products ENABLE ROW LEVEL SECURITY"))
    conn.execute(text("ALTER TABLE products FORCE ROW LEVEL SECURITY"))
    conn.execute(text("CREATE POLICY p ON products USING (true)"))
    assert find_rls_problems(conn) == [
        "products: listed as exempt but is protected; remove the stale exemption"
    ]


def test_every_exemption_names_a_reason() -> None:
    assert all(reason.strip() for reason in EXEMPT.values())
