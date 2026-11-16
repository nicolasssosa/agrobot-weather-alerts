"""Registro de modelos. Alembic importa este paquete para poblar Base.metadata.

TODO(S1): descomentar los imports cuando existan los modelos.
"""

from app.models.base import Base
from app.models.enums import EventType, NotificationStatus

# from app.models.user import User
# from app.models.field import Field
# from app.models.weather import WeatherForecast
# from app.models.alert import Alert
# from app.models.notification import Notification

__all__ = [
    "Base",
    "EventType",
    "NotificationStatus",
    # "User", "Field", "WeatherForecast", "Alert", "Notification",
]
