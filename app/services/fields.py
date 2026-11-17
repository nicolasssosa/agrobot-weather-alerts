"""Casos de uso de campos. Ver docs/API.md §fields.

Todas las funciones filtran por `user_id` (ownership). Un campo de otro usuario
se comporta como inexistente (NotFoundError, nunca 403) para no filtrar IDs.
"""

from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.models import Field
from app.schemas.common import BIGINT_MAX
from app.schemas.field import FieldCreate
from app.services._db_errors import is_unique_violation


async def create_field(
    session: AsyncSession, user_id: int, data: FieldCreate, *, commit: bool = True
) -> Field:
    """Crea un campo del usuario. Nombre duplicado -> `ConflictError`.

    La unicidad se detecta con `flush()` (sqlstate 23505). Con `commit=False` la fila
    queda en la transacción abierta para la idempotencia (docs/API.md §Idempotency-Key).
    """
    field = Field(
        user_id=user_id,
        name=data.name,
        latitude=Decimal(str(data.latitude)),
        longitude=Decimal(str(data.longitude)),
    )
    session.add(field)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        if not is_unique_violation(exc):
            raise
        raise ConflictError("Field name already exists") from None
    if commit:
        await session.commit()
    return field


async def list_fields(
    session: AsyncSession, user_id: int, *, limit: int, offset: int
) -> Sequence[Field]:
    """Lista los campos del usuario ordenados por `id` ASC."""
    result = await session.execute(
        select(Field).where(Field.user_id == user_id).order_by(Field.id).limit(limit).offset(offset)
    )
    return result.scalars().all()


async def get_field(session: AsyncSession, user_id: int, field_id: int) -> Field:
    """Devuelve el campo del usuario o lanza `NotFoundError` si no existe / no es suyo."""
    # Un id fuera del rango BIGINT no puede existir: ni siquiera se consulta (sería un 500).
    if not 1 <= field_id <= BIGINT_MAX:
        raise NotFoundError("Field not found")
    result = await session.execute(
        select(Field).where(Field.id == field_id, Field.user_id == user_id)
    )
    field = result.scalar_one_or_none()
    if field is None:
        raise NotFoundError("Field not found")
    return field
