"""FastAPI dependencies: database connection, injected clock, and the authenticated customer.

`current_customer` is the ONLY source of a customer id for any route. Anything that cannot be
resolved to a live server-side session is a 401 with one fixed body, so a caller learns nothing
about why (unknown, malformed, expired and revoked sessions look identical).
"""

from collections.abc import Iterator
from typing import Annotated
from uuid import UUID

from fastapi import Cookie, Depends, HTTPException, Request
from sqlalchemy import Connection, Engine, create_engine, text

from app.auth.context import CustomerContext
from app.auth.sessions import SESSION_COOKIE, resolve_session
from app.clock import Clock
from app.config import get_settings


def unauthenticated() -> HTTPException:
    return HTTPException(status_code=401, detail="authentication required")


def not_found() -> HTTPException:
    """The ONE 404 for nonexistent AND not-yours records: same status, same body."""
    return HTTPException(status_code=404, detail="not found")


def get_engine(request: Request) -> Engine:
    engine: Engine | None = request.app.state.engine
    if engine is None:  # created lazily so importing the app never connects
        engine = create_engine(get_settings().database_url, pool_pre_ping=True)
        request.app.state.engine = engine
    return engine


def get_clock(request: Request) -> Clock:
    clock: Clock = request.app.state.clock
    return clock


def get_conn(engine: Annotated[Engine, Depends(get_engine)]) -> Iterator[Connection]:
    """One connection per request, in a READ ONLY transaction: these endpoints cannot write even
    if a bug tried to (the database refuses, SQLSTATE 25006)."""
    with engine.connect() as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        yield conn


def current_customer(
    conn: Annotated[Connection, Depends(get_conn)],
    session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> CustomerContext:
    if session is None:
        raise unauthenticated()
    try:
        session_id = UUID(session)
    except ValueError:
        raise unauthenticated() from None
    ctx = resolve_session(conn, session_id)
    if ctx is None:
        raise unauthenticated()
    return ctx
