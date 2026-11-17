"""Router `alerts`. Ver docs/API.md §alerts."""

from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import Response

from app.api.deps import (
    IDEMPOTENCY_KEY_PARAMETER,
    IDEMPOTENT_REPLAYED_HEADER,
    CurrentUser,
    IdempotencyKeyDep,
    SessionDep,
    idempotent_response,
    render_json_body,
)
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
from app.services.idempotency import (
    INVALID_KEY_DETAIL,
    KEY_REUSED_DETAIL,
    MISSING_KEY_DETAIL,
    run_idempotent,
)

router = APIRouter(prefix="/alerts", tags=["alerts"])

#: Alcance de la clave de idempotencia: método + ruta (docs/API.md §Idempotency-Key).
IDEMPOTENCY_SCOPE = "POST /api/v1/alerts"

#: 409 de POST /alerts: unicidad y tope por campo comparten status pero no detalle.
_CREATE_ALERT_409 = {
    "model": ErrorResponse,
    "description": (
        f"{DUPLICATE_ALERT_DETAIL}. También {ALERT_LIMIT_DETAIL} si el campo ya tiene "
        "`MAX_ALERTS_PER_FIELD` (default 10) alertas vivas."
    ),
    "content": {"application/json": {"example": {"detail": DUPLICATE_ALERT_DETAIL}}},
}

#: 422 de POST /alerts: el body inválido responde `HTTPValidationError` (`detail` lista),
#: pero los rechazos de `Idempotency-Key` responden `ErrorResponse` (`detail` string),
#: con el mismo patrón que el 422 de forecasts (docs/API.md §Idempotency-Key).
_CREATE_ALERT_422: Final[dict[str, Any]] = {
    "description": (
        "Cuatro formas según el caso: un body inválido responde `HTTPValidationError` con "
        "`detail` como lista de errores; el header `Idempotency-Key` ausente, mal formado "
        "o reusado con otro body responde `ErrorResponse` con `detail` string."
    ),
    "content": {
        "application/json": {
            "schema": {
                "anyOf": [
                    {"$ref": "#/components/schemas/HTTPValidationError"},
                    {"$ref": "#/components/schemas/ErrorResponse"},
                ]
            },
            "examples": {
                "invalid_body": {
                    "summary": "Body inválido (validación automática de FastAPI)",
                    "value": {
                        "detail": [
                            {
                                "type": "less_than_equal",
                                "loc": ["body", "threshold"],
                                "msg": "Input should be less than or equal to 100",
                                "input": 101,
                                "ctx": {"le": 100},
                            }
                        ]
                    },
                },
                "missing_key": {
                    "summary": "Header `Idempotency-Key` ausente",
                    "value": {"detail": MISSING_KEY_DETAIL},
                },
                "invalid_key": {
                    "summary": "Header `Idempotency-Key` mal formado",
                    "value": {"detail": INVALID_KEY_DETAIL},
                },
                "reused_key": {
                    "summary": "Misma clave con un body distinto",
                    "value": {"detail": KEY_REUSED_DETAIL},
                },
            },
        }
    },
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
        422: _CREATE_ALERT_422,
        201: {"headers": {"Idempotent-Replayed": IDEMPOTENT_REPLAYED_HEADER}},
    },
    openapi_extra={"parameters": [IDEMPOTENCY_KEY_PARAMETER]},
)
async def create_alert_endpoint(
    payload: AlertCreate,
    user: CurrentUser,
    session: SessionDep,
    idempotency_key: IdempotencyKeyDep,
) -> Response:
    """Crea una alerta del usuario autenticado (idempotente por `Idempotency-Key`)."""
    result = await run_idempotent(
        session,
        user_id=user.id,
        scope=IDEMPOTENCY_SCOPE,
        key=idempotency_key,
        request_model=payload,
        operation=lambda: create_alert(session, user.id, payload, commit=False),
        serializer=lambda alert: render_json_body(
            AlertRead.model_validate(alert).model_dump(mode="json")
        ),
        status_code=status.HTTP_201_CREATED,
    )
    return idempotent_response(result)


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
