"""Router `forecasts`. Ver docs/API.md §forecasts.

`GET /api/v1/fields/{field_id}/forecasts` devuelve la lista (sin paginar) de pronósticos
del campo, con filtros opcionales por rango de fechas y tipo de evento.
"""

from collections.abc import Sequence
from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from app.api.deps import CurrentUser, SessionDep
from app.models import EventType, WeatherForecast
from app.schemas.common import protected_responses
from app.schemas.forecast import FORECASTS_422_RESPONSE, ForecastRead
from app.services.forecasts import list_forecasts

router = APIRouter(prefix="/fields", tags=["forecasts"])


@router.get(
    "/{field_id}/forecasts",
    response_model=list[ForecastRead],
    summary="Listar los pronósticos de un campo",
    responses={
        **protected_responses((404, "Field not found")),
        422: FORECASTS_422_RESPONSE,
    },
)
async def list_forecasts_endpoint(
    field_id: int,
    user: CurrentUser,
    session: SessionDep,
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
    event_type: Annotated[EventType | None, Query()] = None,
) -> Sequence[WeatherForecast]:
    """Lista los pronósticos del campo; 404 si no existe o es de otro usuario."""
    if date_from is not None and date_to is not None and date_from > date_to:
        # 422 literal: `status.HTTP_422_UNPROCESSABLE_ENTITY` está deprecado en Starlette
        # reciente y el nombre nuevo no existe en las versiones mínimas soportadas.
        raise HTTPException(
            status_code=422,
            detail="date_from must be <= date_to",
        )
    return await list_forecasts(
        session,
        user.id,
        field_id,
        date_from=date_from,
        date_to=date_to,
        event_type=event_type,
    )
