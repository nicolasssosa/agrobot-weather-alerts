"""Router `notifications`. Ver docs/API.md §notifications."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import CurrentUser, SessionDep
from app.models import NotificationStatus
from app.schemas.common import BIGINT_MAX, PaginationParams, protected_responses
from app.schemas.notification import NotificationPage, NotificationRead
from app.services.notifications import list_notifications

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get(
    "",
    response_model=NotificationPage,
    summary="Listar las notificaciones del usuario",
    responses=protected_responses(),
)
async def list_notifications_endpoint(
    user: CurrentUser,
    session: SessionDep,
    pagination: Annotated[PaginationParams, Depends()],
    status: Annotated[NotificationStatus | None, Query()] = None,
    alert_id: Annotated[int | None, Query(ge=1, le=BIGINT_MAX)] = None,
) -> NotificationPage:
    """Historial paginado de las notificaciones del usuario autenticado."""
    notifications = await list_notifications(
        session,
        user.id,
        status=status,
        alert_id=alert_id,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    return NotificationPage(
        items=[NotificationRead.model_validate(notification) for notification in notifications],
        limit=pagination.limit,
        offset=pagination.offset,
    )
