"""Modelo `WeatherForecast` (tabla `weather_forecasts`).

Ver docs/DATA_MODEL.md §weather_forecasts. Una fila por (campo, día, evento).
"""

from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Enum as SAEnum

from app.models.base import Base, TimestampMixin
from app.models.enums import EventType

if TYPE_CHECKING:
    from app.models.field import Field


class WeatherForecast(Base, TimestampMixin):
    """Pronóstico diario de un evento climático para un campo."""

    __tablename__ = "weather_forecasts"
    __table_args__ = (
        UniqueConstraint("field_id", "forecast_date", "event_type"),
        CheckConstraint("probability BETWEEN 0 AND 100", name="probability_range"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    field_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("fields.id", ondelete="CASCADE"), nullable=False
    )
    forecast_date: Mapped[date] = mapped_column(Date, nullable=False)
    event_type: Mapped[EventType] = mapped_column(
        SAEnum(
            EventType,
            native_enum=False,
            length=32,
            validate_strings=True,
            values_callable=lambda e: [m.value for m in e],
            create_constraint=True,
            name="event_type",
        ),
        nullable=False,
    )
    probability: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    field: Mapped["Field"] = relationship(back_populates="forecasts", lazy="raise")
