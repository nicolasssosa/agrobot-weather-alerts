"""Modelo `Notification` (tabla `notifications`).

Ver docs/DATA_MODEL.md §notifications. Es el outbox que llena el evaluador y consume el dispatcher.
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
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Enum as SAEnum

from app.models.base import Base, TimestampMixin
from app.models.enums import EventType, NotificationStatus

if TYPE_CHECKING:
    from app.models.alert import Alert


class Notification(Base, TimestampMixin):
    """Notificación generada por una alerta para un día pronosticado (snapshot de valores)."""

    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("alert_id", "forecast_date", "episode"),
        CheckConstraint("probability BETWEEN 0 AND 100", name="probability_range"),
        CheckConstraint("threshold BETWEEN 1 AND 100", name="threshold_range"),
        CheckConstraint("episode >= 1", name="episode_positive"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint(
            "last_error IS NULL OR char_length(last_error) <= 1000",
            name="last_error_length",
        ),
        Index(
            "ix_notifications_pending_due",
            "next_attempt_at",
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_notifications_alert_id_created_at", "alert_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    alert_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("alerts.id", ondelete="CASCADE"), nullable=False
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
    threshold: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    #: Número de episodio del (alerta, día) que la generó (S12b, D1): 1 = primer cruce.
    episode: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    status: Mapped[NotificationStatus] = mapped_column(
        SAEnum(
            NotificationStatus,
            native_enum=False,
            length=16,
            validate_strings=True,
            values_callable=lambda e: [m.value for m in e],
            create_constraint=True,
            name="notification_status",
        ),
        nullable=False,
        default=NotificationStatus.PENDING,
        server_default=text("'pending'"),
    )
    attempts: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default=text("0")
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=func.now(), server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    alert: Mapped["Alert"] = relationship(back_populates="notifications", lazy="raise")
