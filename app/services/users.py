"""Casos de uso de usuarios. Ver docs/API.md §users."""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models import User
from app.schemas.user import UserCreate
from app.services._db_errors import is_unique_violation


async def create_user(session: AsyncSession, data: UserCreate) -> User:
    """Crea un usuario. Teléfono duplicado -> `ConflictError`.

    La unicidad se detecta con `flush()` (sqlstate 23505), no con el commit.
    """
    user = User(phone_number=data.phone_number, name=data.name)
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
