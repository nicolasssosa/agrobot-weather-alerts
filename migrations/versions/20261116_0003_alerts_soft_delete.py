"""alerts soft delete

Migración `0003` (docs/DATA_MODEL.md §Migración 0003): borrado lógico de alertas, varios
umbrales por (campo, evento) entre alertas vivas y estado `cancelled` en notificaciones.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _downgrade_incompatibilities() -> list[str]:
    """Describe por qué el downgrade perdería datos (sin borrarlos en silencio)."""
    bind = op.get_bind()
    duplicate_pairs = bind.execute(
        sa.text(
            """
            SELECT count(*)
            FROM (
                SELECT field_id, event_type
                FROM alerts
                GROUP BY field_id, event_type
                HAVING count(*) > 1
            ) AS duplicated
            """
        )
    ).scalar_one()
    cancelled = bind.execute(
        sa.text("SELECT count(*) FROM notifications WHERE status = 'cancelled'")
    ).scalar_one()

    problems: list[str] = []
    if duplicate_pairs:
        problems.append(
            f"{duplicate_pairs} par(es) (field_id, event_type) con más de una alerta "
            "(incluye alertas borradas lógicamente)"
        )
    if cancelled:
        problems.append(f"{cancelled} notificación(es) con status 'cancelled'")
    return problems


def upgrade() -> None:
    op.add_column("alerts", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))

    op.drop_constraint("uq_alerts_field_id_event_type", "alerts", type_="unique")
    op.create_index(
        "uq_alerts_field_event_threshold_alive",
        "alerts",
        ["field_id", "event_type", "threshold"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.drop_index("ix_alerts_active_field_event", table_name="alerts")
    op.create_index(
        "ix_alerts_active_field_event",
        "alerts",
        ["field_id", "event_type"],
        postgresql_where=sa.text("is_active AND deleted_at IS NULL"),
    )

    op.drop_constraint(op.f("ck_notifications_notification_status"), "notifications", type_="check")
    op.create_check_constraint(
        "notification_status",
        "notifications",
        "status IN ('pending', 'sent', 'failed', 'cancelled')",
    )


def downgrade() -> None:
    problems = _downgrade_incompatibilities()
    if problems:
        detail = "; ".join(problems)
        raise RuntimeError(
            "Downgrade de la migración 0003 bloqueado para no borrar datos en silencio: "
            f"{detail}. Eliminá/ajustá esos datos antes de volver a la unicidad "
            "(field_id, event_type) y al status sin 'cancelled'."
        )

    op.drop_constraint(op.f("ck_notifications_notification_status"), "notifications", type_="check")
    op.create_check_constraint(
        "notification_status",
        "notifications",
        "status IN ('pending', 'sent', 'failed')",
    )

    op.drop_index("ix_alerts_active_field_event", table_name="alerts")
    op.create_index(
        "ix_alerts_active_field_event",
        "alerts",
        ["field_id", "event_type"],
        postgresql_where=sa.text("is_active"),
    )

    op.drop_index("uq_alerts_field_event_threshold_alive", table_name="alerts")
    op.create_unique_constraint(
        "uq_alerts_field_id_event_type",
        "alerts",
        ["field_id", "event_type"],
    )

    op.drop_column("alerts", "deleted_at")
