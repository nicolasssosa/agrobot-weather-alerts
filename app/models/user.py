"""Modelo `User` (tabla `users`). Ver docs/DATA_MODEL.md §users."""

from datetime import time
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, CheckConstraint, Identity, String, Time, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.field import DEFAULT_TIMEZONE

if TYPE_CHECKING:
    from app.models.field import Field


class User(Base, TimestampMixin):
    """Usuario de Agrobot, identificado por su teléfono de WhatsApp (E.164)."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(r"phone_number ~ '^\+[1-9][0-9]{7,14}$'", name="phone_number_e164"),
        CheckConstraint(
            "(quiet_hours_start IS NULL) = (quiet_hours_end IS NULL)",
            name="quiet_hours_both_or_none",
        ),
        CheckConstraint("quiet_hours_start <> quiet_hours_end", name="quiet_hours_not_empty"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    phone_number: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    #: Zona IANA del usuario: en ella se interpreta su horario de silencio (S13, D15).
    timezone: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default=DEFAULT_TIMEZONE,
        server_default=text(f"'{DEFAULT_TIMEZONE}'"),
    )
    #: Inicio del silencio, hora local. `None` = sin silencio (CHECK: ambos o ninguno).
    quiet_hours_start: Mapped[time | None] = mapped_column(Time)
    #: Fin (excluido) del silencio; si es menor que el inicio, cruza la medianoche.
    quiet_hours_end: Mapped[time | None] = mapped_column(Time)

    fields: Mapped[list["Field"]] = relationship(
        back_populates="user",
        lazy="raise",
    )
