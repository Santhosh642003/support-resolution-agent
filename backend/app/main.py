from fastapi import FastAPI
from pydantic import BaseModel
from sqlalchemy import Engine

from app.api.chat import router as chat_router
from app.api.routes import router as state_router
from app.clock import Clock, FixedClock, SystemClock
from app.config import get_settings
from app.workflow.order_status import OrderStatusWorkflow


class HealthResponse(BaseModel):
    status: str
    environment: str
    llm_provider: str
    clock_mode: str
    data_is_simulated: bool = True


def create_app(
    *,
    engine: Engine | None = None,
    clock: Clock | None = None,
    workflow: OrderStatusWorkflow | None = None,
) -> FastAPI:
    """Build the application. `engine` and `clock` are injectable (tests pass their own); by
    default the engine is created lazily from settings and the clock follows CLOCK_MODE:
    'simulated' (FixedClock at SIMULATED_TODAY, matching the seeded data) or 'system'."""
    settings = get_settings()
    api = FastAPI(title="Support Resolution Agent (SIMULATED DATA)")
    api.state.engine = engine
    api.state.clock = clock or (SystemClock() if settings.clock_mode == "system" else FixedClock())

    @api.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        s = get_settings()
        return HealthResponse(
            status="ok",
            environment=s.environment,
            llm_provider=s.llm_provider,
            clock_mode=s.clock_mode,
        )

    api.state.workflow = workflow  # None: built lazily from settings on the first /chat
    api.include_router(state_router)
    api.include_router(chat_router)
    return api


app = create_app()
