"""Modelo `User` (tabla `users`). Ver docs/DATA_MODEL.md §users."""

from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, CheckConstraint, Identity, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.field import Field


class User(Base, TimestampMixin):
    """Usuario de Agrobot, identificado por su teléfono de WhatsApp (E.164)."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(r"phone_number ~ '^\+[1-9][0-9]{7,14}$'", name="phone_number_e164"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    phone_number: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)

    fields: Mapped[list["Field"]] = relationship(
        back_populates="user",
        lazy="raise",
    )
