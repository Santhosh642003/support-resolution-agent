"""The authenticated customer. Built ONLY from a validated server-side session (app/auth/
sessions.py); it is never constructed from a request parameter, a body field or model output."""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class CustomerContext:
    customer_id: UUID
