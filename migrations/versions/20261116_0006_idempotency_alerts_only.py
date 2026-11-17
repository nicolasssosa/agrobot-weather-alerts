"""idempotency alerts only

Migración `0006` (docs/DATA_MODEL.md §idempotency_keys, S11b): `Idempotency-Key` pasa a ser
obligatoria y exclusiva de `POST /api/v1/alerts`. Se borran las respuestas cacheadas de
`POST /users` y `POST /fields` (no son datos de negocio), `user_id` pasa a `NOT NULL` y el
UNIQUE se recrea sin `NULLS NOT DISTINCT` (ya no hay filas sin dueño).

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # `ACCESS EXCLUSIVE`: serializa el borrado y el cambio de estructura contra inserts
    # concurrentes de claves (ALTER TABLE posterior también toma este lock).
    op.execute(sa.text("LOCK TABLE idempotency_keys IN ACCESS EXCLUSIVE MODE"))
    # Respuestas cacheadas de endpoints que ya no usan Idempotency-Key: no se recuperan.
    op.execute(sa.text("DELETE FROM idempotency_keys WHERE scope <> 'POST /api/v1/alerts'"))
    op.drop_constraint(
        "uq_idempotency_keys_user_id_scope_key",
        "idempotency_keys",
        type_="unique",
    )
    op.alter_column("idempotency_keys", "user_id", nullable=False)
    op.create_unique_constraint(
        "uq_idempotency_keys_user_id_scope_key",
        "idempotency_keys",
        ["user_id", "scope", "key"],
    )


def downgrade() -> None:
    # Solo revierte la estructura: las filas borradas eran respuestas cacheadas.
    op.drop_constraint(
        "uq_idempotency_keys_user_id_scope_key",
        "idempotency_keys",
        type_="unique",
    )
    op.alter_column("idempotency_keys", "user_id", nullable=True)
    op.create_unique_constraint(
        "uq_idempotency_keys_user_id_scope_key",
        "idempotency_keys",
        ["user_id", "scope", "key"],
        postgresql_nulls_not_distinct=True,
    )
