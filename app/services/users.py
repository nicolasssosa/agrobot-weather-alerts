"""Casos de uso de usuarios. Ver docs/API.md §users."""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models import User
from app.schemas.user import UserCreate

_UNIQUE_VIOLATION_SQLSTATE = "23505"


def _is_unique_violation(exc: IntegrityError) -> bool:
    """Solo las violaciones de unicidad (sqlstate 23505) son un conflicto de negocio."""
    orig = exc.orig
    if getattr(orig, "sqlstate", None) == _UNIQUE_VIOLATION_SQLSTATE:
        return True
    if getattr(orig, "pgcode", None) == _UNIQUE_VIOLATION_SQLSTATE:
        return True
    return getattr(getattr(orig, "__cause__", None), "sqlstate", None) == _UNIQUE_VIOLATION_SQLSTATE


async def create_user(session: AsyncSession, data: UserCreate) -> User:
    """Crea un usuario. Teléfono duplicado -> `ConflictError`."""
    user = User(phone_number=data.phone_number, name=data.name)
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if not _is_unique_violation(exc):
            raise
        raise ConflictError("User with this phone number already exists") from None
    return user


async def get_user(session: AsyncSession, user_id: int) -> User | None:
    """Devuelve el usuario o `None` si no existe."""
    result = await session.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()
