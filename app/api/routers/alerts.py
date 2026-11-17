"""Router `alerts`. Ver docs/API.md §alerts."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import CurrentUser, SessionDep
from app.models import Alert, EventType
from app.schemas.alert import AlertCreate, AlertPage, AlertRead, AlertUpdate
from app.schemas.common import (
    BIGINT_MAX,
    ErrorResponse,
    PaginationParams,
    protected_responses,
)
from app.services.alerts import (
    ALERT_LIMIT_DETAIL,
    DUPLICATE_ALERT_DETAIL,
    create_alert,
    delete_alert,
    get_alert,
    list_alerts,
    update_alert,
)

router = APIRouter(prefix="/alerts", tags=["alerts"])

#: 409 de POST /alerts: unicidad y tope por campo comparten status pero no detalle.
_CREATE_ALERT_409 = {
    "model": ErrorResponse,
    "description": (
        f"{DUPLICATE_ALERT_DETAIL}. También {ALERT_LIMIT_DETAIL} si el campo ya tiene "
        "`MAX_ALERTS_PER_FIELD` (default 10) alertas vivas."
    ),
    "content": {"application/json": {"example": {"detail": DUPLICATE_ALERT_DETAIL}}},
}


@router.post(
    "",
    response_model=AlertRead,
    status_code=status.HTTP_201_CREATED,
    summary="Crear una alerta",
    description=(
        "El `field_id` del ejemplo es un placeholder: reemplazalo por el `id` de un campo "
        "propio (creado con `POST /api/v1/fields`). Un campo inexistente o de otro usuario "
        "responde 404 `Field not found`."
    ),
    responses={
        **protected_responses((404, "Field not found")),
        409: _CREATE_ALERT_409,
    },
)
async def create_alert_endpoint(
    payload: AlertCreate, user: CurrentUser, session: SessionDep
) -> Alert:
    """Crea una alerta del usuario autenticado."""
    return await create_alert(session, user.id, payload)


@router.get(
    "",
    response_model=AlertPage,
    summary="Listar las alertas del usuario",
    responses=protected_responses(),
)
async def list_alerts_endpoint(
    user: CurrentUser,
    session: SessionDep,
    pagination: Annotated[PaginationParams, Depends()],
    field_id: Annotated[int | None, Query(ge=1, le=BIGINT_MAX)] = None,
    event_type: Annotated[EventType | None, Query()] = None,
    is_active: Annotated[bool | None, Query()] = None,
) -> AlertPage:
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
    return AlertPage(
        items=[AlertRead.model_validate(alert) for alert in alerts],
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get(
    "/{alert_id}",
    response_model=AlertRead,
    summary="Obtener una alerta",
    responses=protected_responses((404, "Alert not found")),
)
async def read_alert_endpoint(alert_id: int, user: CurrentUser, session: SessionDep) -> Alert:
    """Devuelve una alerta del usuario; 404 si no existe o es de otro usuario."""
    return await get_alert(session, user.id, alert_id)


@router.patch(
    "/{alert_id}",
    response_model=AlertRead,
    summary="Actualizar una alerta",
    responses=protected_responses(
        (404, "Alert not found"),
        (409, DUPLICATE_ALERT_DETAIL),
    ),
)
async def update_alert_endpoint(
    alert_id: int, payload: AlertUpdate, user: CurrentUser, session: SessionDep
) -> Alert:
    """Actualiza `threshold` y/o `is_active` de una alerta del usuario."""
    return await update_alert(session, user.id, alert_id, payload)


@router.delete(
    "/{alert_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Borrar una alerta",
    responses=protected_responses((404, "Alert not found")),
)
async def delete_alert_endpoint(alert_id: int, user: CurrentUser, session: SessionDep) -> None:
    """Borra lógicamente una alerta propia; 204 sin body (idempotente)."""
    await delete_alert(session, user.id, alert_id)
