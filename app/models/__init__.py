"""Registro de modelos. Alembic importa este paquete para poblar Base.metadata."""

from app.models.alert import Alert
from app.models.base import Base
from app.models.enums import EventType, NotificationStatus
from app.models.field import Field
from app.models.notification import Notification
from app.models.user import User
from app.models.weather import WeatherForecast

__all__ = [
    "Alert",
    "Base",
    "EventType",
    "Field",
    "Notification",
    "NotificationStatus",
    "User",
    "WeatherForecast",
]
