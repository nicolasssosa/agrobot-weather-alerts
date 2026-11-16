"""Historial de notificaciones del usuario (solo lectura). TODO(S6).

async def list_notifications(
    session, user_id: int, *, status: NotificationStatus | None, alert_id: int | None,
    limit: int, offset: int,
) -> Sequence[Notification]
    - JOIN alerts -> fields para filtrar por user_id.
    - Orden: created_at DESC, id DESC.
"""
