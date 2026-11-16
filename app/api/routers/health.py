"""Router `health`. Ver docs/API.md §health."""

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.deps import SessionDep
from app.schemas.common import HealthStatus

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthStatus, responses={503: {"model": HealthStatus}})
async def health(session: SessionDep) -> HealthStatus | JSONResponse:
    """Verifica la conexión con la base con `SELECT 1`."""
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        logger.exception("health check failed error=database_unreachable")
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return HealthStatus(status="ok")
