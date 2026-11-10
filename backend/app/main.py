from fastapi import FastAPI
from pydantic import BaseModel

from app.config import get_settings


class HealthResponse(BaseModel):
    status: str
    environment: str
    llm_provider: str
    data_is_simulated: bool = True


app = FastAPI(title="Support Resolution Agent (SIMULATED DATA)")


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    s = get_settings()
    return HealthResponse(status="ok", environment=s.environment, llm_provider=s.llm_provider)
