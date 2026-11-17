"""Casos de uso de campos. Ver docs/API.md §fields.

Todas las funciones filtran por `user_id` (ownership). Un campo de otro usuario
se comporta como inexistente (NotFoundError, nunca 403) para no filtrar IDs.
"""

from collections.abc import Sequence
from decimal import Decimal
from time import monotonic
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import ConflictError, DomainError, NotFoundError
from app.models import Field, User
from app.schemas.common import BIGINT_MAX
from app.schemas.field import FieldCreate
from app.services._db_errors import is_unique_violation
from app.services.timezones import PG_TIMEZONE_NAMES

#: Detalle del 409 por cuota de campos (docs/API.md §fields, "Límite", S14a).
FIELD_LIMIT_DETAIL: Final = "Field limit reached for this user"

#: TTL del caché por proceso del set de zonas (la tz database cambia con cada release de
#: Postgres/OS: 5 minutos son suficientes para no consultar en cada request).
_TIMEZONE_CACHE_TTL_SECONDS: Final = 300.0

_timezone_names: frozenset[str] | None = None
_timezone_names_at = 0.0


class InvalidTimezoneError(DomainError):
    """Dato inválido del cliente: la API lo mapea a 422 (docs/API.md §fields)."""

    status_code = 422


async def is_supported_timezone(session: AsyncSession, timezone: str) -> bool:
    """¿Postgres reconoce `timezone` con coincidencia exacta? (S12a CR-1/SEC-1).

    El evaluador resuelve el "hoy" de cada campo con `AT TIME ZONE fields.timezone`; una zona
    que Postgres no conoce abortaría la sentencia y dejaría sin evaluar a **todos** los campos.
    Por eso la fuente de verdad es `pg_timezone_names` y no `zoneinfo`. La comparación es
    case-sensitive y byte a byte: `america/argentina/buenos_aires` no es una zona válida.

    El set se cachea por proceso con TTL para no consultar la vista en cada request. No hay
    lock: si el TTL vence con varios requests en vuelo, cada uno hace la misma consulta chica
    (idempotente) y el último set gana; evita atar un `asyncio.Lock` al primer event loop.
    """
    global _timezone_names, _timezone_names_at

    cached = _timezone_names
    if cached is not None and monotonic() - _timezone_names_at < _TIMEZONE_CACHE_TTL_SECONDS:
        return timezone in cached
    result = await session.execute(select(PG_TIMEZONE_NAMES.c.name))
    _timezone_names = frozenset(result.scalars().all())
    _timezone_names_at = monotonic()
    return timezone in _timezone_names


async def _fields_in_use(session: AsyncSession, user_id: int) -> int:
    """Serializa por usuario el conteo y el INSERT de campos (S14a, D16).

    `SELECT ... FROM users WHERE id = :user_id FOR NO KEY UPDATE` deja la fila del usuario
    bloqueada hasta el commit/rollback de esta transacción: dos creaciones concurrentes con un
    solo lugar libre se ordenan y la segunda cuenta el campo de la primera. `FOR NO KEY UPDATE`
    (no `FOR UPDATE`) no bloquea los INSERT de `fields` que validan la FK. Un usuario
    inexistente no bloquea nada: el INSERT falla con la FK, como antes.
    """
    await session.execute(select(User.id).where(User.id == user_id).with_for_update(key_share=True))
    return (
        await session.execute(
            select(func.count()).select_from(Field).where(Field.user_id == user_id)
        )
    ).scalar_one()


async def create_field(session: AsyncSession, user_id: int, data: FieldCreate) -> Field:
    """Crea un campo del usuario. Nombre duplicado -> `ConflictError`.

    Valida la zona contra `pg_timezone_names` antes de insertar: si Postgres no la conoce,
    `InvalidTimezoneError` (422). La unicidad se detecta con `flush()` (sqlstate 23505), no
    con el commit.

    Cuota (S14a, D16): con `MAX_FIELDS_PER_USER` campos, el siguiente -> 409
    `Field limit reached for this user`. Se chequea **antes** que el nombre duplicado y el
    conteo se serializa con el INSERT bloqueando la fila del usuario (`FOR NO KEY UPDATE`).
    """
    if not await is_supported_timezone(session, data.timezone):
        raise InvalidTimezoneError(
            f"Invalid timezone {data.timezone!r}: it is not a timezone recognized by PostgreSQL "
            "(pg_timezone_names)"
        )
    if await _fields_in_use(session, user_id) >= get_settings().max_fields_per_user:
        # Sin rollback explícito: la transacción del llamador se revierte al cerrar la sesión
        # (el 409 no debe descartar trabajo previo del mismo request).
        raise ConflictError(FIELD_LIMIT_DETAIL)
    field = Field(
        user_id=user_id,
        name=data.name,
        latitude=Decimal(str(data.latitude)),
        longitude=Decimal(str(data.longitude)),
        timezone=data.timezone,
    )
    session.add(field)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        if not is_unique_violation(exc):
            raise
        raise ConflictError("Field name already exists") from None
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
