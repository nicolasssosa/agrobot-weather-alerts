"""Router `notifications`. Ver docs/API.md §notifications."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import CurrentUser, SessionDep
from app.models import NotificationStatus
from app.schemas.common import BIGINT_MAX, Page, PaginationParams
from app.schemas.notification import NotificationRead
from app.services.notifications import list_notifications

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", response_model=Page[NotificationRead])
async def list_notifications_endpoint(
    user: CurrentUser,
    session: SessionDep,
    pagination: Annotated[PaginationParams, Depends()],
    status: Annotated[NotificationStatus | None, Query()] = None,
    alert_id: Annotated[int | None, Query(ge=1, le=BIGINT_MAX)] = None,
) -> Page[NotificationRead]:
    """Historial paginado de las notificaciones del usuario autenticado."""
    notifications = await list_notifications(
        session,
        user.id,
        status=status,
        alert_id=alert_id,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    return Page[NotificationRead](
        items=[NotificationRead.model_validate(notification) for notification in notifications],
        limit=pagination.limit,
        offset=pagination.offset,
    )
