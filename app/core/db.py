"""Engine y sesiones async de SQLAlchemy.

Convención de transacciones:
- La API obtiene la sesión vía `get_session` (dependency). Los *services* hacen
  `await session.commit()` explícitamente en operaciones de escritura.
- Los jobs abren su propia sesión con `get_sessionmaker()` y commitean al final
  de cada paso (evaluación / lote de despacho).
"""

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings


@lru_cache
def get_engine() -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        echo=settings.db_echo,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
    )


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency. Los tests la reemplazan con `app.dependency_overrides`."""
    async with get_sessionmaker()() as session:
        yield session
