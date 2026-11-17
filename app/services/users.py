"""Casos de uso de usuarios. Ver docs/API.md §users."""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models import User
from app.schemas.user import UserCreate, UserUpdate
from app.services._db_errors import is_unique_violation
from app.services.fields import InvalidTimezoneError, is_supported_timezone


async def _ensure_supported_timezone(session: AsyncSession, timezone: str) -> None:
    """Valida la zona contra `pg_timezone_names` (mismo 422 que `POST /fields`, S13).

    La columna no tiene CHECK de zona: sin esta validación, una zona que PostgreSQL no
    reconoce rompería el cálculo del horario de silencio del usuario.
    """
    if not await is_supported_timezone(session, timezone):
        raise InvalidTimezoneError(
            f"Invalid timezone {timezone!r}: it is not a timezone recognized by PostgreSQL "
            "(pg_timezone_names)"
        )


async def create_user(session: AsyncSession, data: UserCreate) -> User:
    """Crea un usuario. Teléfono duplicado -> `ConflictError`; zona desconocida -> 422.

    La unicidad se detecta con `flush()` (sqlstate 23505), no con el commit.
    """
    await _ensure_supported_timezone(session, data.timezone)
    user = User(
        phone_number=data.phone_number,
        name=data.name,
        timezone=data.timezone,
        quiet_hours_start=data.quiet_hours.start if data.quiet_hours else None,
        quiet_hours_end=data.quiet_hours.end if data.quiet_hours else None,
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        if not is_unique_violation(exc):
            raise
        raise ConflictError("User with this phone number already exists") from None
    await session.commit()
    return user


async def get_user(session: AsyncSession, user_id: int) -> User | None:
    """Devuelve el usuario o `None` si no existe."""
    result = await session.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def update_user(session: AsyncSession, user: User, data: UserUpdate) -> User:
    """Actualiza solo los campos presentes en el PATCH de `/users/me`; commit en el service.

    Se distinguen los campos omitidos de los enviados con `model_fields_set`: omitir
    `quiet_hours` no lo cambia, y un `null` explícito lo desactiva (API.md §users). La zona
    se valida contra `pg_timezone_names` antes de escribir (zona desconocida -> 422).
    """
    if "timezone" in data.model_fields_set and data.timezone is not None:
        await _ensure_supported_timezone(session, data.timezone)
        user.timezone = data.timezone
    if "quiet_hours" in data.model_fields_set:
        user.quiet_hours_start = data.quiet_hours.start if data.quiet_hours else None
        user.quiet_hours_end = data.quiet_hours.end if data.quiet_hours else None
    await session.commit()
    return user
