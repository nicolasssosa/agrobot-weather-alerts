"""Punto de entrada ASGI: `uvicorn app.main:app`."""

from fastapi import FastAPI

from app.api.routers import alerts, fields, forecasts, health, notifications, users
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging

API_PREFIX = "/api/v1"


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(
        title="Agrobot - Alertas Climáticas",
        version="0.1.0",
        description="Ver docs/API.md",
    )
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(users.router, prefix=API_PREFIX)
    app.include_router(fields.router, prefix=API_PREFIX)
    app.include_router(forecasts.router, prefix=API_PREFIX)
    app.include_router(alerts.router, prefix=API_PREFIX)
    app.include_router(notifications.router, prefix=API_PREFIX)
    return app


app = create_app()
