"""Modelo `IdempotencyKey` (tabla `idempotency_keys`). Ver docs/DATA_MODEL.md §idempotency_keys."""

from datetime import datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class IdempotencyKey(Base):
    """Respuesta guardada de un POST con header `Idempotency-Key` (docs/API.md §Idempotency-Key)."""

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        # `NULLS NOT DISTINCT` (PostgreSQL >= 15): `user_id` es NULL en `POST /users` y
        # dos claves iguales de ese endpoint también tienen que colisionar.
        UniqueConstraint(
            "user_id",
            "scope",
            "key",
            name="uq_idempotency_keys_user_id_scope_key",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_idempotency_keys_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    scope: Mapped[str] = mapped_column(String(64), nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    status_code: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    # TEXT con los bytes exactos de la respuesta (replay byte a byte); en `POST /users`
    # guarda solo `{"id": …}`, sin PII (docs/DATA_MODEL.md §idempotency_keys).
    response_body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
