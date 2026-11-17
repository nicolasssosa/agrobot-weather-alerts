"""alert episodes

Migración `0008` (docs/DATA_MODEL.md §Migración 0008, S12b/D1): episodios de notificación
por (alerta, día pronosticado).

- Crea `alert_day_states` (estado de re-aviso: último episodio y si está armado).
- Agrega `notifications.episode` (default 1: las filas existentes quedan en el episodio 1).
- Reemplaza `UNIQUE (alert_id, forecast_date)` por
  `UNIQUE (alert_id, forecast_date, episode)`.
- **Backfill:** una fila de `alert_day_states` por cada (alerta, día) ya notificado, con
  `episode = max(episode)` y `armed = false`, para que el despliegue no re-avise días que ya
  se avisaron: se comportan como un episodio en curso.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _downgrade_incompatibilities() -> list[str]:
    """Describe por qué el downgrade perdería episodios (sin borrarlos en silencio)."""
    bind = op.get_bind()
    renotified = bind.execute(
        sa.text("SELECT count(*) FROM notifications WHERE episode > 1")
    ).scalar_one()

    problems: list[str] = []
    if renotified:
        problems.append(f"{renotified} notificación(es) con episode > 1")
    return problems


def upgrade() -> None:
    op.create_table(
        "alert_day_states",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("alert_id", sa.BigInteger(), nullable=False),
        sa.Column("forecast_date", sa.Date(), nullable=False),
        sa.Column("episode", sa.Integer(), nullable=False),
        sa.Column("armed", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("episode >= 1", name="episode_positive"),
        sa.ForeignKeyConstraint(
            ["alert_id"],
            ["alerts.id"],
            name="fk_alert_day_states_alert_id_alerts",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_alert_day_states"),
        sa.UniqueConstraint(
            "alert_id",
            "forecast_date",
            name="uq_alert_day_states_alert_id_forecast_date",
        ),
    )

    op.add_column(
        "notifications",
        sa.Column("episode", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )
    op.create_check_constraint("episode_positive", "notifications", "episode >= 1")

    op.drop_constraint("uq_notifications_alert_id_forecast_date", "notifications", type_="unique")
    op.create_unique_constraint(
        "uq_notifications_alert_id_forecast_date_episode",
        "notifications",
        ["alert_id", "forecast_date", "episode"],
    )

    # Backfill: un día ya notificado sigue "arriba" (desarmado), así no se re-avisa al
    # desplegar. `armed = false` con `episode = max(episode)` es exactamente el estado del
    # último episodio disparado.
    op.execute(
        sa.text(
            """
            INSERT INTO alert_day_states (alert_id, forecast_date, episode, armed)
            SELECT alert_id, forecast_date, max(episode), false
            FROM notifications
            GROUP BY alert_id, forecast_date
            """
        )
    )


def downgrade() -> None:
    problems = _downgrade_incompatibilities()
    if problems:
        detail = "; ".join(problems)
        raise RuntimeError(
            "Downgrade de la migración 0008 bloqueado para no borrar episodios en silencio: "
            f"{detail}. Eliminá esas notificaciones (o volvé a episodio 1) antes de restaurar "
            "la unicidad (alert_id, forecast_date) y eliminar alert_day_states."
        )

    op.drop_constraint(
        "uq_notifications_alert_id_forecast_date_episode", "notifications", type_="unique"
    )
    op.create_unique_constraint(
        "uq_notifications_alert_id_forecast_date",
        "notifications",
        ["alert_id", "forecast_date"],
    )
    op.drop_constraint(op.f("ck_notifications_episode_positive"), "notifications", type_="check")
    op.drop_column("notifications", "episode")
    op.drop_table("alert_day_states")
