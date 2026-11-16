"""Modelo `Alert` (tabla `alerts`). Ver docs/DATA_MODEL.md §alerts."""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    SmallInteger,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Enum as SAEnum

from app.models.base import Base, TimestampMixin
from app.models.enums import EventType

if TYPE_CHECKING:
    from app.models.field import Field
    from app.models.notification import Notification


class Alert(Base, TimestampMixin):
    """Configuración "avisame cuando `event_type` en el campo sea >= `threshold`"."""

    __tablename__ = "alerts"
    __table_args__ = (
        Index(
            "uq_alerts_field_event_threshold_alive",
            "field_id",
            "event_type",
            "threshold",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        CheckConstraint("threshold BETWEEN 1 AND 100", name="threshold_range"),
        Index(
            "ix_alerts_active_field_event",
            "field_id",
            "event_type",
            postgresql_where=text("is_active AND deleted_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    field_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("fields.id", ondelete="CASCADE"), nullable=False
    )
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
    threshold: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=true(), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    field: Mapped["Field"] = relationship(back_populates="alerts", lazy="raise")
    notifications: Mapped[list["Notification"]] = relationship(
        back_populates="alert",
        lazy="raise",
    )
