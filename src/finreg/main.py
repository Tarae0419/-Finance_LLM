"""API foundation. Readiness does not claim that legal data or a model is ready."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from finreg.config import Settings
from finreg.db import check_database, create_database_engine


class HealthResponse(BaseModel):
    status: Literal["ok", "not_ready"]
    database: Literal["not_checked", "not_configured", "available", "unavailable"]


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_database_engine(config)
        app.state.database_engine = engine
        try:
            yield
        finally:
            if engine is not None:
                engine.dispose()

    app = FastAPI(title="FinReg API", version="0.1.0", lifespan=lifespan)

    @app.get("/health/live", response_model=HealthResponse, tags=["health"])
    def live() -> HealthResponse:
        return HealthResponse(status="ok", database="not_checked")

    @app.get(
        "/health/ready",
        response_model=HealthResponse,
        responses={503: {"model": HealthResponse}},
        tags=["health"],
    )
    def ready(request: Request) -> HealthResponse | JSONResponse:
        engine = request.app.state.database_engine
        if engine is None:
            return JSONResponse(
                status_code=503,
                content=HealthResponse(status="not_ready", database="not_configured").model_dump(),
            )
        try:
            check_database(engine)
        except SQLAlchemyError:
            # Do not expose DSNs, connection errors, or driver messages to clients.
            return JSONResponse(
                status_code=503,
                content=HealthResponse(status="not_ready", database="unavailable").model_dump(),
            )
        return HealthResponse(status="ok", database="available")

    return app
