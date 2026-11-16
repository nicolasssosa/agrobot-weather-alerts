"""Schemas Pydantic v2 de `notification`. Ver docs/API.md §notifications.

`last_error` y `next_attempt_at` son internos del dispatcher: no se exponen en la API.
"""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict

from app.models import EventType, NotificationStatus


class NotificationRead(BaseModel):
    """Respuesta del historial de notificaciones del usuario."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    alert_id: int
    forecast_date: date
    event_type: EventType
    probability: int
    threshold: int
    status: NotificationStatus
    attempts: int
    sent_at: datetime | None
    created_at: datetime
