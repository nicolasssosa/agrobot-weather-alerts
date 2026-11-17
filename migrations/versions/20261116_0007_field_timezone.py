"""field timezone

Migración `0007` (docs/DATA_MODEL.md §Migración 0007, S12a): `fields.timezone` guarda la zona
IANA del campo y define su "hoy" local (D9). Se agrega `NOT NULL` con server default, así las
filas existentes toman `America/Argentina/Buenos_Aires` sin backfill explícito.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_TIMEZONE = "America/Argentina/Buenos_Aires"


def upgrade() -> None:
    op.add_column(
        "fields",
        sa.Column(
            "timezone",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text(f"'{_DEFAULT_TIMEZONE}'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("fields", "timezone")
