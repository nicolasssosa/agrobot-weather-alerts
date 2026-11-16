"""Schemas Pydantic v2 de `notification`. Ver docs/API.md §notifications.

`last_error` y `next_attempt_at` son internos del dispatcher: no se exponen en la API.
"""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict

from app.models import EventType, NotificationStatus
from app.schemas.common import Page


class NotificationRead(BaseModel):
    """Respuesta del historial de notificaciones del usuario."""

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    "id": 10,
                    "alert_id": 1,
                    "forecast_date": "2026-11-18",
                    "event_type": "frost",
                    "probability": 85,
                    "threshold": 70,
                    "status": "sent",
                    "attempts": 1,
                    "sent_at": "2026-11-18T12:00:05Z",
                    "created_at": "2026-11-18T12:00:00Z",
                }
            ],
        },
    )

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


class NotificationPage(Page[NotificationRead]):
    """Respuesta paginada de `GET /api/v1/notifications`."""
