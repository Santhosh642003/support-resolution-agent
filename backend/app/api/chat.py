"""POST /chat: one customer message in, one verified reply out (order-status workflow, M1).

Identity comes only from the session cookie: there is no customer identifier in the path, the
query, the body (unknown fields are REJECTED, so a smuggled `customer_id` is a 422, not ignored)
or the response. The reply and every value in it come from the determination and the templates:
the model's own stated outcome or figures are never returned. The turn trace is persisted for
every request the workflow handles, including failed and fail-closed turns.
"""

from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection

from app.api.deps import current_customer, get_conn, get_workflow, not_found
from app.auth.context import CustomerContext
from app.workflow.order_status import ConversationNotFound, OrderStatusWorkflow

router = APIRouter()


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=2000)
    conversation_id: UUID | None = None  # continue a conversation; must be yours or it is a 404


class CitationOut(BaseModel):
    clause_key: str  # from the determination's applied clause keys, never from the model
    label: str
    text: str
    role: Literal["decisive", "supporting"]


class DeterminationOut(BaseModel):
    outcome: str
    reason_codes: list[str]
    facts: dict[str, str | int | bool | None]
    decisive_clause_keys: list[str]
    supporting_clause_keys: list[str]
    policy_versions: dict[str, int]
    authorizes_action: bool  # always false for an order-status determination


class ChatResponse(BaseModel):
    conversation_id: UUID
    turn_no: int
    trace_id: str
    state: str
    reply: str  # the explanation text (or a template) plus the simulation notice
    citations: list[CitationOut]
    determination: DeterminationOut | None
    handoff_reason: str | None
    degraded: bool
    as_of: date
    data_as_of: datetime
    simulated: bool = True


Conn = Annotated[Connection, Depends(get_conn)]
Customer = Annotated[CustomerContext, Depends(current_customer)]
Workflow = Annotated[OrderStatusWorkflow, Depends(get_workflow)]


@router.post("/chat", response_model=ChatResponse)
def chat(body: ChatRequest, conn: Conn, ctx: Customer, workflow: Workflow) -> ChatResponse:
    try:
        r = workflow.run_turn(conn, ctx, body.message, conversation_id=body.conversation_id)
    except ConversationNotFound:
        raise not_found() from None  # not found and not yours are the same 404
    det = r.determination
    decisive = set(det["decisive_clause_keys"]) if det else set()
    return ChatResponse(
        conversation_id=r.conversation_id,
        turn_no=r.turn_no,
        trace_id=r.trace_id,
        state=r.state.value,
        reply=r.reply,
        citations=[
            CitationOut(
                clause_key=c.clause_key,
                label=c.label,
                text=c.text,
                role="decisive" if c.clause_key in decisive else "supporting",
            )
            for c in r.citations
        ],
        determination=None
        if det is None
        else DeterminationOut(
            outcome=det["outcome"],
            reason_codes=det["reason_codes"],
            facts=det["facts"],
            decisive_clause_keys=det["decisive_clause_keys"],
            supporting_clause_keys=det["supporting_clause_keys"],
            policy_versions=det["policy_versions"],
            authorizes_action=det["authorizes_action"],
        ),
        handoff_reason=r.handoff_reason,
        degraded=r.degraded,
        as_of=r.as_of,
        data_as_of=r.data_as_of,
    )
