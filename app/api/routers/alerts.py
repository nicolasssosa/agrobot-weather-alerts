"""Router `alerts`. Ver docs/API.md §alerts."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import CurrentUser, SessionDep
from app.models import Alert, EventType
from app.schemas.alert import AlertCreate, AlertRead, AlertUpdate
from app.schemas.common import BIGINT_MAX, Page, PaginationParams
from app.services.alerts import (
    create_alert,
    delete_alert,
    get_alert,
    list_alerts,
    update_alert,
)

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.post("", response_model=AlertRead, status_code=status.HTTP_201_CREATED)
async def create_alert_endpoint(
    payload: AlertCreate, user: CurrentUser, session: SessionDep
) -> Alert:
    """Crea una alerta del usuario autenticado."""
    return await create_alert(session, user.id, payload)


@router.get("", response_model=Page[AlertRead])
async def list_alerts_endpoint(
    user: CurrentUser,
    session: SessionDep,
    pagination: Annotated[PaginationParams, Depends()],
    field_id: Annotated[int | None, Query(ge=1, le=BIGINT_MAX)] = None,
    event_type: Annotated[EventType | None, Query()] = None,
    is_active: Annotated[bool | None, Query()] = None,
) -> Page[AlertRead]:
    """Lista paginada de las alertas del usuario autenticado."""
    alerts = await list_alerts(
        session,
        user.id,
        field_id=field_id,
        event_type=event_type,
        is_active=is_active,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    return Page[AlertRead](
        items=[AlertRead.model_validate(alert) for alert in alerts],
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get("/{alert_id}", response_model=AlertRead)
async def read_alert_endpoint(alert_id: int, user: CurrentUser, session: SessionDep) -> Alert:
    """Devuelve una alerta del usuario; 404 si no existe o es de otro usuario."""
    return await get_alert(session, user.id, alert_id)


@router.patch("/{alert_id}", response_model=AlertRead)
async def update_alert_endpoint(
    alert_id: int, payload: AlertUpdate, user: CurrentUser, session: SessionDep
) -> Alert:
    """Actualiza `threshold` y/o `is_active` de una alerta del usuario."""
    return await update_alert(session, user.id, alert_id, payload)


@router.delete("/{alert_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_alert_endpoint(alert_id: int, user: CurrentUser, session: SessionDep) -> None:
    """Borra una alerta del usuario; 204 sin body."""
    await delete_alert(session, user.id, alert_id)
