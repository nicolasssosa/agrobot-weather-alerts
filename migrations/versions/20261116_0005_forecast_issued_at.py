"""forecast issued_at

Migración `0005` (docs/DATA_MODEL.md §Migración 0005): `weather_forecasts.issued_at` guarda
cuándo emitió el proveedor el pronóstico (S11, D14). Se agrega nullable, se completa con
`fetched_at` para las filas existentes y se pasa a `NOT NULL`.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "weather_forecasts",
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Las filas viejas no tienen emisión conocida: la mejor aproximación es la ingesta.
    op.execute(
        sa.text("UPDATE weather_forecasts SET issued_at = fetched_at WHERE issued_at IS NULL")
    )
    op.alter_column("weather_forecasts", "issued_at", nullable=False)


def downgrade() -> None:
    op.drop_column("weather_forecasts", "issued_at")
