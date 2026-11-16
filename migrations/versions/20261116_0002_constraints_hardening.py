"""constraints hardening

Migración `0002` (docs/DATA_MODEL.md §Migración 0002): CHECK E.164 en
`users.phone_number` y tope de 1000 caracteres en `notifications.last_error`
(follow-ups S1 SEC-1 y SEC-2).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa  # noqa: F401  (se conserva la plantilla de Alembic)
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_check_constraint(
        "phone_number_e164",
        "users",
        r"phone_number ~ '^\+[1-9][0-9]{7,14}$'",
    )
    op.create_check_constraint(
        "last_error_length",
        "notifications",
        "last_error IS NULL OR char_length(last_error) <= 1000",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_notifications_last_error_length"), "notifications", type_="check")
    op.drop_constraint(op.f("ck_users_phone_number_e164"), "users", type_="check")
