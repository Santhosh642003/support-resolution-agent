"""Row-level security and the application role (SPEC 17).

EVERY test here that is about isolation connects as `support_app`, never as the bootstrap
superuser: a test that passes as a superuser proves nothing, because a superuser bypasses the
policies. The one test that connects as the superuser does so deliberately, to document the
boundary (test_a_superuser_bypasses_the_policies_and_that_is_why_the_app_must_not_be_one).
"""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import DBAPIError

from app.auth.context import CustomerContext
from app.auth.sessions import SESSION_COOKIE, create_session
from app.clock import FixedClock
from app.db.rls import PrivilegedRoleError, assert_unprivileged_role, set_current_customer
from app.main import create_app
from app.providers.guard import FakePromptGuard
from app.repositories import state as repo
from app.retrieval.embedder import OnnxBgeEmbedder
from app.retrieval.ingest import ingest_policies
from app.retrieval.policy_loader import load_policies
from app.seed import ids
from app.seed.loader import seed_all
from tests.db.conftest import (
    APP_PASSWORD,
    OWNER_PASSWORD,
    _role_url,
    admin_base_url,
    make_database,
)
from tests.providers.helpers import ScriptedAdapter
from tests.workflow.helpers import build_workflow, explanation_reply, extraction_reply

ALEX, BLAIR = ids.customer_id("alex"), ids.customer_id("blair")
C2 = "shipping-v1-clause-2"
GOOD = explanation_reply("presumed_lost", [C2], "It shipped on 2026-03-11 and is presumed lost.")

# Tables that are DELIBERATELY not row-level protected, with the reason. A table with a customer_id
# column that is not protected, or a table that is neither protected nor listed here, fails the
# catalog test below: protection (or a stated exemption) is required of every future table.
UNPROTECTED = {
    "alembic_version": "migration bookkeeping, no customer data",
    "products": "shared reference data, read-only to the app role",
    "policy_documents": "shared reference data, read-only to the app role",
    "policy_chunks": "shared reference data, read-only to the app role",
    "policy_incorporations": "shared reference data, read-only to the app role",
    # Columns are (provider text, day date, requests integer): counters, no text of any kind, so it
    # cannot hold customer data. (llm_cache and llm_calls CAN, and are protected: migration 0013.)
    "llm_usage_daily": "global provider quota counters; no text column, cannot hold customer data",
}

# Protected tables whose policy also admits NULL-customer rows (shared). A "returns no rows with no
# customer set" assertion on these counts only the rows that belong to someone.
SHARED_NULL_TABLES = {"llm_cache", "llm_calls"}


@dataclass
class World:
    name: str
    superuser: Engine
    owner: Engine
    app: Engine  # pool of exactly one connection, so reuse between requests is forced
    llm: Engine  # support_app with a normal pool: the cache and call log use their own connections
    sessions: dict[str, UUID]
    embedder: OnnxBgeEmbedder

    def client(self, who: str | None, script: list[Any] | None = None) -> TestClient:
        adapter = ScriptedAdapter(
            script if script is not None else [extraction_reply("SIM-2001"), GOOD]
        )
        wf = build_workflow(adapter, self.embedder, guard=FakePromptGuard())
        # NO dependency overrides: the real get_conn / current_customer / get_workflow chain, on a
        # real pooled connection as support_app.
        c = TestClient(create_app(engine=self.app, clock=FixedClock(), workflow=wf))
        if who:
            c.cookies.set(SESSION_COOKIE, str(self.sessions[who]))
        return c


@pytest.fixture(scope="module")
def world() -> Iterator[World]:
    embedder = OnnxBgeEmbedder()
    with make_database() as (name, su):
        with su.begin() as c:  # bootstrap data, as the bootstrap superuser
            seed_all(c)
            ingest_policies(c, embedder, load_policies())
            sessions = {n: create_session(c, ids.customer_id(n)) for n in ("alex", "blair")}
        base = admin_base_url()
        app = create_engine(
            _role_url(base, name, "support_app", APP_PASSWORD), pool_size=1, max_overflow=0
        )
        owner = create_engine(_role_url(base, name, "support_owner", OWNER_PASSWORD))
        llm = create_engine(_role_url(base, name, "support_app", APP_PASSWORD))
        yield World(name, su, owner, app, llm, sessions, embedder)
        llm.dispose()
        app.dispose()
        owner.dispose()


@contextmanager
def as_customer(engine: Engine, customer: UUID | None) -> Iterator[Connection]:
    """One transaction on `engine`, scoped to `customer` (or to nobody), rolled back at the end."""
    with engine.connect() as c:
        trans = c.begin()
        try:
            if customer is not None:
                set_current_customer(c, customer)
            yield c
        finally:
            trans.rollback()


def protected_tables(c: Connection) -> list[str]:
    return [
        r[0]
        for r in c.execute(
            text(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relrowsecurity ORDER BY 1"
            )
        )
    ]


def sqlstate(exc: DBAPIError) -> str | None:
    return getattr(exc.orig, "sqlstate", None)


# --- the roles themselves --------------------------------------------------------------------------


def test_the_application_role_is_neither_superuser_nor_bypassrls_nor_an_owner(
    world: World,
) -> None:
    with world.superuser.connect() as c:
        rows = {
            r.rolname: r
            for r in c.execute(
                text(
                    "SELECT rolname, rolsuper, rolbypassrls, rolcanlogin FROM pg_roles"
                    " WHERE rolname IN ('support_app', 'support_owner')"
                )
            )
        }
        for role in ("support_app", "support_owner"):
            assert (
                rows[role].rolcanlogin and not rows[role].rolsuper and not rows[role].rolbypassrls
            )
        owned_by_app = c.execute(
            text(
                "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE n.nspname = 'public' AND pg_get_userbyid(c.relowner) = 'support_app'"
            )
        ).scalar_one()
        assert owned_by_app == 0
        tables = c.execute(
            text(
                "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'"
                " AND tableowner <> 'support_owner'"
            )
        ).scalar_one()
        assert tables == 0  # the owner role owns every table (and so the migrations were run by it)


def test_the_startup_check_accepts_the_app_role_and_refuses_the_superuser_and_the_owner(
    world: World,
) -> None:
    with world.app.connect() as c:
        assert_unprivileged_role(c)  # no exception
    with world.superuser.connect() as c, pytest.raises(PrivilegedRoleError, match="superuser"):
        assert_unprivileged_role(c)
    with world.owner.connect() as c, pytest.raises(PrivilegedRoleError, match="owns"):
        assert_unprivileged_role(c)


# --- the policies bite ------------------------------------------------------------------------------


def test_a_superuser_bypasses_the_policies_and_that_is_why_the_app_must_not_be_one(
    world: World,
) -> None:
    """DELIBERATELY connects as the bootstrap superuser, to document the boundary: with no customer
    set it still sees every customer's rows. The same query as support_app sees none."""
    q = "SELECT count(DISTINCT customer_id) FROM orders"
    with as_customer(world.superuser, None) as c:
        assert c.execute(text(q)).scalar_one() >= 3  # alex, blair, carol (and any other)
    with as_customer(world.app, None) as c:
        assert c.execute(text(q)).scalar_one() == 0


def test_the_owner_is_also_subject_to_the_policies_because_of_force(world: World) -> None:
    with as_customer(world.owner, None) as c:
        assert c.execute(text("SELECT count(*) FROM orders")).scalar_one() == 0
        for t in protected_tables(c):
            where = " WHERE customer_id IS NOT NULL" if t in SHARED_NULL_TABLES else ""
            assert c.execute(text(f"SELECT count(*) FROM {t}{where}")).scalar_one() == 0, t  # noqa: S608


def test_with_no_customer_set_every_protected_table_returns_no_rows(world: World) -> None:
    with as_customer(world.app, None) as c:
        for t in protected_tables(c):
            # a table the app role may not read at all raises 42501; the rest must be empty
            where = " WHERE customer_id IS NOT NULL" if t in SHARED_NULL_TABLES else ""
            try:
                with c.begin_nested():
                    n = c.execute(text(f"SELECT count(*) FROM {t}{where}")).scalar_one()  # noqa: S608
            except DBAPIError as exc:
                assert sqlstate(exc) == "42501", (t, exc)
                continue
            assert n == 0, t


def test_an_unfiltered_query_returns_only_the_current_customers_rows(world: World) -> None:
    for me, other in ((ALEX, BLAIR), (BLAIR, ALEX)):
        with as_customer(world.app, me) as c:
            owners = {r[0] for r in c.execute(text("SELECT DISTINCT customer_id FROM orders"))}
            assert owners == {me} and other not in owners
            for t in ("order_items", "subscriptions", "shipments", "shipment_events"):
                got = {r[0] for r in c.execute(text(f"SELECT DISTINCT customer_id FROM {t}"))}  # noqa: S608
                assert got <= {me}, t
            who = {r[0] for r in c.execute(text("SELECT id FROM customers"))}
            assert who == {me}


def test_an_insert_for_another_customer_is_refused_by_the_database(world: World) -> None:
    with as_customer(world.app, ALEX) as c:
        c.execute(
            text("INSERT INTO conversations (customer_id) VALUES (:c)"), {"c": ALEX}
        )  # own: ok
        with pytest.raises(DBAPIError) as exc:
            c.execute(text("INSERT INTO conversations (customer_id) VALUES (:c)"), {"c": BLAIR})
        assert sqlstate(exc.value) == "42501"  # new row violates row-level security policy


def test_the_app_role_holds_no_write_on_business_tables(world: World) -> None:
    with as_customer(world.app, ALEX) as c:
        for sql in (
            "UPDATE orders SET status = status",
            "DELETE FROM orders",
            "INSERT INTO refunds (id) VALUES (gen_random_uuid())",
            "UPDATE subscriptions SET status = status",
            "TRUNCATE orders",
        ):
            with pytest.raises(DBAPIError) as exc, c.begin_nested():
                c.execute(text(sql))
            assert sqlstate(exc.value) == "42501", sql


def test_the_decision_policies_link_table_follows_its_decision(world: World) -> None:
    r = world.client("blair").post("/chat", json={"message": "Where is my order SIM-2001?"})
    assert r.status_code == 200
    with as_customer(world.app, BLAIR) as c:
        assert c.execute(text("SELECT count(*) FROM decision_policies")).scalar_one() >= 1
    with as_customer(world.app, ALEX) as c:  # no customer_id column: visible only via its decision
        assert c.execute(text("SELECT count(*) FROM decision_policies")).scalar_one() == 0


# --- the setting is transaction-scoped, never session-scoped -------------------------------------


def test_a_pooled_connection_carries_no_customer_into_the_next_request(world: World) -> None:
    ids_seen = []
    with world.app.connect() as c1:
        ids_seen.append(id(c1.connection.driver_connection))
        with c1.begin():
            set_current_customer(c1, ALEX)
            assert c1.execute(text("SELECT count(*) FROM orders")).scalar_one() > 0
    with world.app.connect() as c2:  # pool_size=1: the SAME DBAPI connection again
        ids_seen.append(id(c2.connection.driver_connection))
        setting = c2.execute(
            text("SELECT current_setting('app.current_customer_id', true)")
        ).scalar()
        assert not setting  # '' or NULL, never alex's id
        assert c2.execute(text("SELECT count(*) FROM orders")).scalar_one() == 0
    assert ids_seen[0] == ids_seen[1], (
        "the pool did not reuse the connection: the test proves nothing"
    )


def test_the_setting_is_discarded_on_commit_and_on_rollback(world: World) -> None:
    with world.app.connect() as c:
        for end in ("commit", "rollback"):
            set_current_customer(c, ALEX)
            getattr(c, end)()
            got = c.execute(
                text("SELECT current_setting('app.current_customer_id', true)")
            ).scalar()
            assert not got, end
            c.rollback()


def test_no_application_code_uses_a_session_scoped_setting() -> None:
    """Static guard: the only place the customer setting is written is app/db/rls.py, and it is
    always transaction-local (set_config(..., true)); there is no SET and no is_local=false."""
    app_dir = Path(__file__).resolve().parents[2] / "app"
    offenders = []
    for path in app_dir.rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        if re.search(r"(?i)\bSET\s+(?:SESSION\s+)?app\.", src) or re.search(
            r"set_config\([^)]*,\s*false\s*\)", src, re.IGNORECASE
        ):
            offenders.append(str(path.relative_to(app_dir)))
        if "set_config(" in src and path.name not in (
            "rls.py",
            "bootstrap.py",
        ):  # bootstrap: passwords
            offenders.append(f"{path.relative_to(app_dir)} (set_config outside rls.py)")
    assert not offenders, offenders


def test_set_local_refuses_a_connection_where_it_would_silently_do_nothing(world: World) -> None:
    with world.app.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        with pytest.raises(RuntimeError, match="did not persist"):
            set_current_customer(c, ALEX)


# --- the catalog: every protected table, the exact policies, and no table slips through -------


def test_every_table_with_a_customer_id_is_forced_and_has_a_policy_and_the_rest_are_listed(
    world: World,
) -> None:
    with world.superuser.connect() as c:
        rows = c.execute(
            text(
                "SELECT c.relname, c.relrowsecurity AS rls, c.relforcerowsecurity AS forced,"
                " EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid"
                "   AND a.attname = 'customer_id' AND NOT a.attisdropped) AS has_customer,"
                " (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) AS policies"
                " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE n.nspname = 'public' AND c.relkind = 'r' ORDER BY 1"
            )
        ).all()
    protected = {r.relname for r in rows if r.rls}
    for r in rows:
        if r.has_customer:
            assert r.rls and r.forced and r.policies >= 1, f"{r.relname} has customer_id but no RLS"
    assert {"customers", "decision_policies"} <= protected
    for r in rows:
        if r.rls:
            assert r.forced and r.policies >= 1, r.relname
        else:
            assert r.relname in UNPROTECTED, f"{r.relname}: neither protected nor listed as exempt"
    assert not (set(UNPROTECTED) & protected)  # an exemption that is no longer true must go


def test_the_policy_definitions_are_exactly_these(world: World) -> None:
    with world.superuser.connect() as c:
        pol = {
            (r.tablename, r.policyname): r
            for r in c.execute(
                text(
                    "SELECT tablename, policyname, cmd, qual, with_check FROM pg_policies"
                    " WHERE schemaname = 'public'"
                )
            )
        }
    expected_expr = "(customer_id = ( SELECT app_current_customer() AS app_current_customer))"
    for t in ("orders", "conversations", "decisions", "actions", "refunds"):
        p = pol[(t, "customer_isolation")]
        assert (p.cmd, p.qual, p.with_check) == ("ALL", expected_expr, expected_expr), t
    current = "(SELECT app_current_customer() AS app_current_customer)"
    audit = pol[("audit_log", "customer_isolation")]  # NULL is not writable, stated in the policy
    assert audit.qual == expected_expr
    assert audit.with_check == f"((customer_id IS NOT NULL) AND (customer_id = ( {current[1:-1]})))"
    shared = f"((customer_id IS NULL) OR (customer_id = ( {current[1:-1]})))"
    for t in ("llm_cache", "llm_calls"):
        assert pol[(t, "customer_isolation")].qual == shared, t
    assert pol[("llm_cache", "customer_isolation")].with_check == shared
    assert pol[("llm_calls", "customer_isolation")].with_check == (
        f"((customer_id = ( {current[1:-1]})) OR ((customer_id IS NULL) AND (error_detail IS NULL)))"
    )
    p = pol[("customers", "customer_isolation")]
    assert p.qual == "(id = ( SELECT app_current_customer() AS app_current_customer))"
    assert "decisions" in pol[("decision_policies", "customer_isolation")].qual
    s = pol[("auth_sessions", "session_lookup")]
    assert s.cmd == "SELECT" and "app_presented_session" in s.qual


# --- the mutation test: remove the repository filter, RLS alone must still deny ----------------


def strip_customer_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate the bug RLS exists for: the repository forgets its `customer_id = :c` filters."""
    real_text = text

    def mutated(sql: str) -> object:
        return real_text(
            re.sub(r"\b\w+\.customer_id = :c AND ", "", sql).replace(
                " WHERE customer_id = :c AND ", " WHERE "
            )
        )

    monkeypatch.setattr(repo, "text", mutated)


def test_with_the_repository_filter_removed_rls_alone_still_denies_the_read(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    blairs_order = "SIM-2001"
    # CONTROL: the mutation really removes the protection. On the superuser connection (which
    # bypasses RLS) the mutated repository hands ALEX the order of BLAIR.
    strip_customer_filter(monkeypatch)
    with as_customer(world.superuser, None) as c:
        leaked = repo.get_order(c, CustomerContext(ALEX), blairs_order)
        assert leaked is not None and leaked.order_number == blairs_order, "mutation had no effect"
        assert repo.order_snapshot(c, CustomerContext(ALEX), blairs_order) is not None
    # THE PROOF: the same mutated code as support_app, scoped to alex, denies it.
    with as_customer(world.app, ALEX) as c:
        assert repo.get_order(c, CustomerContext(ALEX), blairs_order) is None
        assert repo.order_snapshot(c, CustomerContext(ALEX), blairs_order) is None
        assert all(
            o.order_number != blairs_order for o in repo.list_orders(c, CustomerContext(ALEX))
        )
    # ... and the mutated code is still correct for the customer's own data.
    with as_customer(world.app, BLAIR) as c:
        assert repo.get_order(c, CustomerContext(BLAIR), blairs_order) is not None


# --- the whole request path, as support_app, with no dependency overrides ------------------


def test_a_read_endpoint_serves_only_the_session_customers_orders(world: World) -> None:
    r = world.client("alex").get("/orders")
    assert r.status_code == 200
    numbers = {o["order_number"] for o in r.json()["orders"]}
    assert numbers and "SIM-2001" not in numbers
    assert world.client("alex").get("/orders/SIM-2001").status_code == 404  # blair's
    assert world.client("blair").get("/orders/SIM-2001").status_code == 200


def test_session_resolution_works_under_rls_and_a_bad_session_is_401(world: World) -> None:
    c = world.client(None)
    assert c.get("/orders").status_code == 401  # no cookie
    c.cookies.set(SESSION_COOKIE, "00000000-0000-0000-0000-000000000000")
    assert c.get("/orders").status_code == 401  # well-formed, unknown
    c.cookies.set(SESSION_COOKIE, "not-a-uuid")
    assert c.get("/orders").status_code == 401


def test_a_session_lookup_reveals_no_other_session_row(world: World) -> None:
    with as_customer(world.app, None) as c:
        assert c.execute(text("SELECT count(*) FROM auth_sessions")).scalar_one() == 0
        from app.db.rls import present_session

        present_session(c, world.sessions["alex"])
        rows = c.execute(text("SELECT customer_id FROM auth_sessions")).all()
        assert [r.customer_id for r in rows] == [ALEX]  # exactly the presented one


def test_a_chat_turn_runs_and_writes_its_record_as_the_app_role(world: World) -> None:
    r = world.client("blair").post("/chat", json={"message": "Where is my order SIM-2001?"})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["state"] == "ANSWERED" and b["determination"]["outcome"] == "presumed_lost"
    cid = UUID(b["conversation_id"])
    with world.superuser.connect() as c:  # read back with the superuser, bypassing RLS
        n = {
            t: c.execute(
                text(f"SELECT count(*) FROM {t} WHERE conversation_id = :i"),  # noqa: S608
                {"i": cid},
            ).scalar_one()
            for t in ("messages", "turn_traces", "decisions")
        }
        assert n["messages"] == 2 and n["turn_traces"] == 1 and n["decisions"] == 1
        owner = c.execute(
            text("SELECT customer_id FROM conversations WHERE id = :i"), {"i": cid}
        ).scalar_one()
        assert owner == BLAIR


def test_another_customers_conversation_is_invisible_to_the_database_itself(world: World) -> None:
    mine = world.client("blair").post("/chat", json={"message": "Where is my order SIM-2001?"})
    cid = mine.json()["conversation_id"]
    with as_customer(world.app, ALEX) as c:
        # the filter-less query: only RLS stands between alex and blair's conversation
        assert (
            c.execute(
                text("SELECT count(*) FROM conversations WHERE id = :i"), {"i": cid}
            ).scalar_one()
            == 0
        )
        assert (
            c.execute(
                text("SELECT count(*) FROM messages WHERE conversation_id = :i"), {"i": cid}
            ).scalar_one()
            == 0
        )


def test_the_request_transaction_resets_between_two_customers_on_one_connection(
    world: World,
) -> None:
    """alex then blair then alex through the real dependency chain on a pool of ONE connection."""
    for who, expect_orders in (("alex", True), ("blair", True), ("alex", True)):
        r = world.client(who).get("/orders")
        mine = {o["order_number"] for o in r.json()["orders"]}
        assert bool(mine) is expect_orders
        assert ("SIM-2001" in mine) is (who == "blair")


def test_the_application_refuses_to_start_on_a_privileged_role(world: World) -> None:
    app = create_app(clock=FixedClock())  # no engine: lazily built from settings by the lifespan
    # the lifespan builds its engine from DATABASE_URL; point it at the superuser for this check
    from app.config import get_settings

    settings = get_settings()
    original = settings.database_url
    settings.database_url = str(world.superuser.url.render_as_string(hide_password=False))
    try:
        with pytest.raises(PrivilegedRoleError):
            with TestClient(app):
                pass
    finally:
        settings.database_url = original


# --- migration 0013: audit_log NULL, llm_cache, llm_calls (SPEC 17.7) -----------------------------


def test_the_app_role_cannot_write_an_audit_row_without_a_customer(world: World) -> None:
    ins = (
        "INSERT INTO audit_log (actor_type, customer_id, event, entity_type, entity_id)"
        " VALUES ('system', :c, 'probe', 'order', gen_random_uuid())"
    )
    with as_customer(world.app, ALEX) as c:
        c.execute(text(ins), {"c": ALEX})  # own customer: allowed
        for who in (None, BLAIR):  # NULL, and another customer's id: both refused
            with pytest.raises(DBAPIError) as exc, c.begin_nested():
                c.execute(text(ins), {"c": who})
            assert sqlstate(exc.value) == "42501", who
    with as_customer(world.app, None) as c:  # no customer bound: still cannot write a NULL row
        with pytest.raises(DBAPIError) as exc:
            c.execute(text(ins), {"c": None})
        assert sqlstate(exc.value) == "42501"
    with as_customer(world.superuser, None) as c:  # the boundary: only a bypassing role can
        c.execute(text(ins), {"c": None})


def _client(world: World, mode: Any = None, *, script: list[Any] | None = None) -> Any:
    from app.providers.cache import CacheMode
    from app.providers.client import LLMClient

    adapter = ScriptedAdapter(script if script is not None else [])
    return adapter, LLMClient(adapter, engine=world.llm, cache_mode=mode or CacheMode.READ_WRITE)


def _req(customer: UUID | None, text_: str = "cache probe") -> Any:
    from app.providers.base import CallContext, LLMRequest, Message

    return LLMRequest(
        messages=(Message(role="user", content=text_),),
        context=CallContext(customer_id=customer),
    )


def test_a_cached_response_is_tagged_with_its_customer_and_shared_only_when_untagged(
    world: World,
) -> None:
    from app.providers.base import CacheMiss
    from app.providers.cache import CacheMode
    from tests.providers.helpers import reply

    text_ = "alex only: where is order SIM-2001? (cache rls probe a1)"
    adapter, live = _client(world, script=[reply("alex-private"), reply("shared-answer")])
    live.complete(_req(ALEX, text_))  # written inside alex's transaction
    live.complete(_req(None, "no customer data in this prompt (cache rls probe a2)"))
    with world.superuser.connect() as c:
        tags = dict(
            c.execute(
                text(
                    "SELECT response->>'text', customer_id FROM llm_cache WHERE provider = 'scripted'"
                    " AND response->>'text' IN ('alex-private', 'shared-answer')"
                )
            ).all()
        )
    assert tags == {"alex-private": ALEX, "shared-answer": None}

    # Same key, three readers (replay_only: a miss raises and makes NO call).
    _, replay = _client(world, CacheMode.REPLAY_ONLY)
    assert replay.complete(_req(ALEX, text_)).text == "alex-private"
    with pytest.raises(CacheMiss):
        replay.complete(_req(BLAIR, text_))  # blair cannot see alex's row
    with pytest.raises(CacheMiss):
        replay.complete(_req(None, text_))  # and neither can a request with no customer
    shared = "no customer data in this prompt (cache rls probe a2)"
    assert replay.complete(_req(BLAIR, shared)).text == "shared-answer"  # NULL rows are shared
    assert replay.complete(_req(ALEX, shared)).text == "shared-answer"
    assert adapter.calls == 2  # the replays made no provider call


def test_eval_replay_reads_each_cases_own_rows_across_customers_with_zero_calls(
    world: World,
) -> None:
    """The eval runner executes each case as that case's (seeded) customer, so it binds the
    customer per case exactly as the request path does; every case replays from the cache."""
    from app.providers.cache import CacheMode
    from tests.providers.helpers import reply

    cases = [(ALEX, "replay probe alex (b1)", "A"), (BLAIR, "replay probe blair (b2)", "B")]
    adapter, live = _client(world, script=[reply("A"), reply("B")])
    for who, msg, _ in cases:
        live.complete(_req(who, msg))
    adapter2, replay = _client(world, CacheMode.REPLAY_ONLY)
    got = [replay.complete(_req(who, msg)).text for who, msg, _ in cases]
    assert got == ["A", "B"] and adapter2.calls == 0


def test_the_database_refuses_a_cache_or_call_row_for_another_customer(world: World) -> None:
    ins_cache = (
        "INSERT INTO llm_cache (cache_key, provider, model, kind, prompt_template_version,"
        " tool_schema_version, response, adapter_calls, customer_id)"
        " VALUES (:k, 'p', 'm', 'complete', 'v', 'v', '{}'::jsonb, 1, :c)"
    )
    with as_customer(world.app, ALEX) as c:
        c.execute(text(ins_cache), {"k": "rls-own", "c": ALEX})
        c.execute(text(ins_cache), {"k": "rls-shared", "c": None})
        with pytest.raises(DBAPIError) as exc, c.begin_nested():
            c.execute(text(ins_cache), {"k": "rls-other", "c": BLAIR})
        assert sqlstate(exc.value) == "42501"
    with as_customer(world.app, BLAIR) as c:
        seen = {r[0] for r in c.execute(text("SELECT cache_key FROM llm_cache"))}
        assert "rls-own" not in seen and "rls-other" not in seen
    with as_customer(world.owner, ALEX) as c:  # FORCE: the owner is subject to it too
        c.execute(text(ins_cache), {"k": "rls-owner-ok", "c": ALEX})
        with pytest.raises(DBAPIError) as exc, c.begin_nested():
            c.execute(text(ins_cache), {"k": "rls-owner-bad", "c": BLAIR})
        assert sqlstate(exc.value) == "42501"


def test_a_call_log_row_with_an_error_body_must_belong_to_a_customer(world: World) -> None:
    """A provider error body can echo the prompt or the failed output, so it can hold customer text:
    it is never writable on a shared (NULL) row."""
    ins = (
        "INSERT INTO llm_calls (trace_id, provider, model, cache_status, outcome, error_kind,"
        " error_detail, customer_id) VALUES (:t, 'p', 'm', 'miss', :o, :k, :d, :c)"
    )
    with as_customer(world.app, ALEX) as c:
        c.execute(text(ins), {"t": "t1", "o": "error", "k": "X", "d": "echoed text", "c": ALEX})
        c.execute(text(ins), {"t": "t2", "o": "ok", "k": None, "d": None, "c": None})
        for bad in (
            {"t": "t3", "o": "error", "k": "X", "d": "echoed text", "c": None},
            {"t": "t4", "o": "error", "k": "X", "d": "echoed text", "c": BLAIR},
        ):
            with pytest.raises(DBAPIError) as exc, c.begin_nested():
                c.execute(text(ins), bad)
            assert sqlstate(exc.value) == "42501", bad["t"]
        with pytest.raises(DBAPIError) as exc, c.begin_nested():  # no SELECT grant either
            c.execute(text("SELECT error_detail FROM llm_calls"))
        assert sqlstate(exc.value) == "42501"
    with as_customer(world.owner, BLAIR) as c:  # another customer's error body is invisible
        assert (
            c.execute(text("SELECT count(*) FROM llm_calls WHERE trace_id = 't1'")).scalar_one()
            == 0
        )


def test_a_chat_turn_tags_every_cache_and_call_row_with_the_session_customer(world: World) -> None:
    """The workflow, not the test, supplies the customer: both model calls of a real /chat turn."""
    from app.providers.cache import CacheMode
    from app.providers.client import LLMClient

    adapter = ScriptedAdapter([extraction_reply("SIM-2001"), GOOD])
    llm = LLMClient(adapter, engine=world.llm, cache_mode=CacheMode.READ_WRITE)
    wf = build_workflow(adapter, world.embedder, guard=FakePromptGuard(), client=llm)
    app_ = TestClient(create_app(engine=world.app, clock=FixedClock(), workflow=wf))
    app_.cookies.set(SESSION_COOKIE, str(world.sessions["blair"]))
    msg = "Where is my order SIM-2001? (rls tagging probe c1)"
    with world.superuser.connect() as c:
        first_new = c.execute(text("SELECT coalesce(max(id), 0) + 1 FROM llm_calls")).scalar_one()
    r = app_.post("/chat", json={"message": msg})
    assert r.status_code == 200, r.text
    assert adapter.calls == 2
    with world.superuser.connect() as c:
        cache = c.execute(
            text(
                "SELECT customer_id FROM llm_cache WHERE provider = 'scripted' AND adapter_calls > 0"
                " AND response->>'text' LIKE '%SIM-2001%'"
            )
        ).all()
        assert cache and {r_.customer_id for r_ in cache} == {BLAIR}
        calls = c.execute(
            text("SELECT customer_id FROM llm_calls WHERE id >= :first"), {"first": first_new}
        ).all()
        assert len(calls) == 2 and {r_.customer_id for r_ in calls} == {BLAIR}
