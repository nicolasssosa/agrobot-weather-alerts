"""Historial de notificaciones del usuario (solo lectura). Ver docs/API.md §notifications.

`notifications` no tiene `user_id`: el dueño se deriva de `alerts -> fields.user_id`, así que
toda query hace esos JOINs y filtra por el usuario autenticado.
"""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Alert, Field, Notification, NotificationStatus


async def list_notifications(
    session: AsyncSession,
    user_id: int,
    *,
    status: NotificationStatus | None,
    alert_id: int | None,
    limit: int,
    offset: int,
) -> Sequence[Notification]:
    """Lista las notificaciones del usuario, con filtros opcionales, más nuevas primero."""
    query = (
        select(Notification)
        .join(Alert, Alert.id == Notification.alert_id)
        .join(Field, Field.id == Alert.field_id)
        .where(Field.user_id == user_id)
    )
    if status is not None:
        query = query.where(Notification.status == status)
    if alert_id is not None:
        query = query.where(Notification.alert_id == alert_id)
    query = (
        query.order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(query)
    return result.scalars().all()
