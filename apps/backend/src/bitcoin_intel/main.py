from __future__ import annotations

from typing import Literal

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

from bitcoin_intel.core.config import Settings, get_settings


class HealthResponse(BaseModel):
    status: Literal["ok"]


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the Phase 0 API application."""

    resolved_settings = settings or get_settings()
    application = FastAPI(
        title=resolved_settings.app_name,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @application.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(status="ok")

    return application


app = create_app()


def run() -> None:
    """Run the API with validated environment configuration."""

    settings = get_settings()
    uvicorn.run(
        "bitcoin_intel.main:app",
        host=settings.app_host,
        port=settings.app_port,
        log_level=settings.log_level.lower(),
    )
