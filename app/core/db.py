"""Engine y sesiones async de SQLAlchemy.

Convención de transacciones:
- La API obtiene la sesión vía `get_session` (dependency). Los *services* hacen
  `await session.commit()` explícitamente en operaciones de escritura.
- Los jobs abren su propia sesión con `get_sessionmaker()` y commitean al final
  de cada paso (evaluación / lote de despacho).

`build_engine` aplica los timeouts de Postgres (S14a) a **todas** las conexiones del engine:
`statement_timeout` y `lock_timeout`, en milisegundos (`0` = sin límite). Una espera por un
lock o una consulta que se pasa de tiempo termina en un error con sqlstate `55P03` / `57014`,
que la API traduce a 503 (`app/core/errors.py`). Las migraciones (`migrations/env.py`) usan
su propio engine y no cambian.
"""

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings


def build_engine(settings: Settings) -> AsyncEngine:
    """Crea el engine async con los timeouts de Postgres de `settings` (S14a).

    `server_settings` los aplica asyncpg al abrir cada conexión, así valen para la API y para
    el worker sin cambiar cada consulta. `statement_timeout` acota una consulta colgada y
    `lock_timeout` una espera por un lock: en vez de colgar el request, el cliente recibe un
    503 y puede reintentar.
    """
    return create_async_engine(
        settings.database_url,
        echo=settings.db_echo,
        # Nunca loguear parámetros de los statements (pueden contener PII).
        hide_parameters=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        connect_args={
            "server_settings": {
                "statement_timeout": str(settings.db_statement_timeout_ms),
                "lock_timeout": str(settings.db_lock_timeout_ms),
            }
        },
    )


@lru_cache
def get_engine() -> AsyncEngine:
    return build_engine(get_settings())


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency. Los tests la reemplazan con `app.dependency_overrides`."""
    async with get_sessionmaker()() as session:
        yield session
