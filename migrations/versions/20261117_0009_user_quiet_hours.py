"""horario de silencio por usuario

Migración `0009` (docs/DATA_MODEL.md §Migración 0009, S13/D15): zona horaria y horario de
silencio del usuario.

- `users.timezone VARCHAR(64) NOT NULL DEFAULT 'America/Argentina/Buenos_Aires'` (server
  default: las filas existentes la toman sin backfill).
- `users.quiet_hours_start/end TIME NULL` (sin silencio por defecto).
- CHECKs `quiet_hours_both_or_none` (o los dos extremos o ninguno) y `quiet_hours_not_empty`
  (una ventana vacía no tiene sentido).

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_TIMEZONE = "America/Argentina/Buenos_Aires"


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "timezone",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text(f"'{_DEFAULT_TIMEZONE}'"),
        ),
    )
    op.add_column("users", sa.Column("quiet_hours_start", sa.Time(), nullable=True))
    op.add_column("users", sa.Column("quiet_hours_end", sa.Time(), nullable=True))
    op.create_check_constraint(
        "quiet_hours_both_or_none",
        "users",
        "(quiet_hours_start IS NULL) = (quiet_hours_end IS NULL)",
    )
    op.create_check_constraint(
        "quiet_hours_not_empty",
        "users",
        "quiet_hours_start <> quiet_hours_end",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_users_quiet_hours_not_empty"), "users", type_="check")
    op.drop_constraint(op.f("ck_users_quiet_hours_both_or_none"), "users", type_="check")
    op.drop_column("users", "quiet_hours_end")
    op.drop_column("users", "quiet_hours_start")
    op.drop_column("users", "timezone")
