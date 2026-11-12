"""Row-level security plumbing (SPEC 17): bind the authenticated customer to the TRANSACTION, and
refuse to run as a role that would make row-level security a silent no-op.

`set_config(name, value, true)` is `SET LOCAL` with a bound parameter: the value lives until the
end of the current transaction and is discarded at COMMIT or ROLLBACK. It is deliberately never
the session-scoped form (`SET`, or is_local=false): session state outlives the request on a pooled
connection and would carry one customer's identity into the next request, turning the control
meant to prevent a cross-customer leak into the cause of one.
"""

from uuid import UUID

from sqlalchemy import Connection, text

CUSTOMER_SETTING = "app.current_customer_id"
SESSION_SETTING = "app.session_id"


class PrivilegedRoleError(RuntimeError):
    """The connection's role bypasses row-level security, so isolation would silently not apply."""


def _set_local(conn: Connection, name: str, value: str) -> None:
    conn.execute(text("SELECT set_config(:n, :v, true)"), {"n": name, "v": value})
    # Read it back in the NEXT statement: if the connection is in autocommit mode each statement is
    # its own transaction, the setting is already gone, and isolation would silently not apply.
    got = conn.execute(text("SELECT current_setting(:n, true)"), {"n": name}).scalar_one()
    if (got or "") != value:
        raise RuntimeError(
            f"{name} did not persist into the next statement: the request must run inside ONE "
            "transaction (not autocommit), or row-level security would not see the customer"
        )


def present_session(conn: Connection, session_id: UUID) -> None:
    """Let the session lookup see the one session row presented (policy session_lookup)."""
    _set_local(conn, SESSION_SETTING, str(session_id))


def clear_presented_session(conn: Connection) -> None:
    _set_local(conn, SESSION_SETTING, "")


def set_current_customer(conn: Connection, customer_id: UUID) -> None:
    """Scope this transaction to one customer. Called with an AUTHENTICATED session's id only."""
    _set_local(conn, CUSTOMER_SETTING, str(customer_id))


def assert_unprivileged_role(conn: Connection) -> None:
    """Fail loudly if the current role is a superuser, has BYPASSRLS, or owns a table in `public`:
    each of those makes row-level security do nothing, with no error to say so."""
    row = conn.execute(
        text(
            "SELECT r.rolname, r.rolsuper, r.rolbypassrls,"
            " (SELECT count(*) FROM pg_tables t WHERE t.schemaname = 'public'"
            "   AND t.tableowner = r.rolname) AS owned"
            " FROM pg_roles r WHERE r.rolname = current_user"
        )
    ).one()
    problems = [
        label
        for label, bad in (
            ("is a superuser", row.rolsuper),
            ("has BYPASSRLS", row.rolbypassrls),
            (f"owns {row.owned} table(s)", row.owned > 0),
        )
        if bad
    ]
    if problems:
        raise PrivilegedRoleError(
            f"the application's database role {row.rolname!r} {' and '.join(problems)}, which "
            "bypasses row-level security. Connect as support_app (docs/DEPLOYMENT.md, SPEC 17)."
        )
