"""Server-side sessions. The cookie carries an opaque session id; the customer id is looked up
from the auth_sessions row, never taken from the request.

Session expiry uses the DATABASE wall clock (clock_timestamp()), like confirmation-token expiry:
it is operational time, not business-date logic, so it is deliberately not the injected Clock.

NOT built here: a login endpoint (verifying a password and issuing a session). Sessions are
created by `create_session`, which tests and the future login route use.
"""

from uuid import UUID

from sqlalchemy import Connection, text

from app.auth.context import CustomerContext

SESSION_COOKIE = "session"
DEFAULT_TTL_SECONDS = 3600


def create_session(
    conn: Connection, customer_id: UUID, ttl_seconds: int = DEFAULT_TTL_SECONDS
) -> UUID:
    session_id: UUID = conn.execute(
        text(
            "INSERT INTO auth_sessions (customer_id, expires_at)"
            " VALUES (:c, clock_timestamp() + make_interval(secs => :ttl)) RETURNING id"
        ),
        {"c": customer_id, "ttl": ttl_seconds},
    ).scalar_one()
    return session_id


def resolve_session(conn: Connection, session_id: UUID) -> CustomerContext | None:
    """The customer for a live (unrevoked, unexpired) session, else None."""
    row = conn.execute(
        text(
            "SELECT customer_id FROM auth_sessions"
            " WHERE id = :i AND revoked_at IS NULL AND expires_at > clock_timestamp()"
        ),
        {"i": session_id},
    ).one_or_none()
    return CustomerContext(row.customer_id) if row else None
