"""Estado de episodio por (alerta, día pronosticado). Ver docs/DATA_MODEL.md §alert_day_states.

Lo escribe el evaluador (re-armado y disparo) y lo re-arma el service de alertas cuando un
PATCH cambia el umbral o reactiva la alerta. **No hay fila = armada, episodio 0.**
"""

from datetime import date

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Identity,
    Integer,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class AlertDayState(Base, TimestampMixin):
    """Último episodio disparado para un día, y si el día vuelve a estar armado."""

    __tablename__ = "alert_day_states"
    __table_args__ = (
        UniqueConstraint("alert_id", "forecast_date"),
        CheckConstraint("episode >= 1", name="episode_positive"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    alert_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("alerts.id", ondelete="CASCADE"), nullable=False
    )
    forecast_date: Mapped[date] = mapped_column(Date, nullable=False)
    episode: Mapped[int] = mapped_column(Integer, nullable=False)
    armed: Mapped[bool] = mapped_column(Boolean, nullable=False)
