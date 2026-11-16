"""Modelo `Field` (tabla `fields`). Ver docs/DATA_MODEL.md §fields."""

from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Identity,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.alert import Alert
    from app.models.user import User
    from app.models.weather import WeatherForecast


class Field(Base, TimestampMixin):
    """Campo (lote/establecimiento) perteneciente a un usuario."""

    __tablename__ = "fields"
    __table_args__ = (
        UniqueConstraint("user_id", "name"),
        CheckConstraint("latitude BETWEEN -90 AND 90", name="latitude_range"),
        CheckConstraint("longitude BETWEEN -180 AND 180", name="longitude_range"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    latitude: Mapped[Decimal] = mapped_column(Numeric(9, 6), nullable=False)
    longitude: Mapped[Decimal] = mapped_column(Numeric(9, 6), nullable=False)

    user: Mapped["User"] = relationship(back_populates="fields", lazy="raise")
    forecasts: Mapped[list["WeatherForecast"]] = relationship(
        back_populates="field",
        lazy="raise",
    )
    alerts: Mapped[list["Alert"]] = relationship(back_populates="field", lazy="raise")
