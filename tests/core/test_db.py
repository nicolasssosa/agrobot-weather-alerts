"""Tests de `build_engine`: timeouts de Postgres por conexión (S14a).

Ver docs/BACKLOG.md §S14a y docs/API.md §Convenciones ("Base de datos ocupada"): los
`DB_STATEMENT_TIMEOUT_MS` / `DB_LOCK_TIMEOUT_MS` se aplican como `server_settings` de
asyncpg a **todas** las conexiones del engine de la app, y `0` deja el límite desactivado
(valor de Postgres).
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core import db as db_module
from app.core.config import Settings
from app.core.db import build_engine, get_engine, get_sessionmaker
from tests.conftest import TEST_DATABASE_URL


def _settings(**overrides: object) -> Settings:
    """Settings contra la base de test con los timeouts indicados."""
    return Settings(database_url=TEST_DATABASE_URL, **overrides)  # type: ignore[arg-type]


async def _timeouts(conn: AsyncConnection) -> tuple[str, str]:
    """`SHOW statement_timeout` y `SHOW lock_timeout`, tal como los devuelve Postgres."""
    statement = (await conn.execute(text("SHOW statement_timeout"))).scalar_one()
    lock = (await conn.execute(text("SHOW lock_timeout"))).scalar_one()
    return statement, lock


async def test_build_engine_applies_the_configured_timeouts() -> None:
    """`SHOW` refleja los settings configurados (no los defaults)."""
    engine = build_engine(_settings(db_statement_timeout_ms=1234, db_lock_timeout_ms=567))
    try:
        async with engine.connect() as conn:
            assert await _timeouts(conn) == ("1234ms", "567ms")
    finally:
        await engine.dispose()


async def test_build_engine_applies_the_documented_defaults() -> None:
    """Defaults del contrato: 30 s de statement y 5 s de lock (docs/BACKLOG.md §S14a)."""
    settings = _settings()
    assert settings.db_statement_timeout_ms == 30000
    assert settings.db_lock_timeout_ms == 5000

    engine = build_engine(settings)
    try:
        async with engine.connect() as conn:
            assert await _timeouts(conn) == ("30s", "5s")
    finally:
        await engine.dispose()


async def test_zero_means_no_limit() -> None:
    """`0` = sin límite: Postgres lo muestra como `0` (statement y lock)."""
    engine = build_engine(_settings(db_statement_timeout_ms=0, db_lock_timeout_ms=0))
    try:
        async with engine.connect() as conn:
            assert await _timeouts(conn) == ("0", "0")
    finally:
        await engine.dispose()


async def test_every_connection_of_the_engine_gets_the_timeouts() -> None:
    """Los timeouts valen para todas las conexiones del pool, no solo la primera."""
    engine = build_engine(_settings(db_statement_timeout_ms=2000, db_lock_timeout_ms=1500))
    try:
        async with engine.connect() as first, engine.connect() as second:
            assert first is not second
            for conn in (first, second):
                assert await _timeouts(conn) == ("2s", "1500ms")
    finally:
        await engine.dispose()


async def test_get_engine_uses_build_engine(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`get_engine()` (API y worker) es el engine de `build_engine(settings)`."""
    settings = _settings(db_statement_timeout_ms=4321, db_lock_timeout_ms=321)
    monkeypatch.setattr(db_module, "get_settings", lambda: settings)
    db_module.get_engine.cache_clear()
    db_module.get_sessionmaker.cache_clear()
    try:
        engine = get_engine()
        assert engine is get_sessionmaker().kw["bind"]
        async with engine.connect() as conn:
            assert await _timeouts(conn) == ("4321ms", "321ms")
    finally:
        await engine.dispose()
        db_module.get_engine.cache_clear()
        db_module.get_sessionmaker.cache_clear()
