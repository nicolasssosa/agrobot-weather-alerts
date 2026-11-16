"""Tests de la migración inicial (docs/TESTING.md §test_migrations).

El `upgrade`/`downgrade` de `0001` lo ejecuta la fixture de sesión de `conftest.py` antes de
cualquier test; acá se verifica que el esquema migrado y los modelos no divergen.
"""

import asyncio
import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parent.parent
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/agrobot_test",
)


def test_alembic_check_reports_no_differences() -> None:
    """`alembic check` sobre la base migrada: modelos y esquema coinciden."""
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.check(cfg)


async def test_initial_migration_creates_all_tables(db_session: AsyncSession) -> None:
    result = await db_session.execute(
        text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    )
    tables = set(result.scalars().all())
    assert {"users", "fields", "weather_forecasts", "alerts", "notifications"} <= tables


def _config() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    return cfg


def test_migration_0002_upgrade_and_downgrade_are_clean() -> None:
    """La migración `0002` se revierte y se vuelve a aplicar sin errores."""
    cfg = _config()

    command.downgrade(cfg, "0001")
    command.upgrade(cfg, "head")


def test_migration_0003_upgrade_and_downgrade_are_clean() -> None:
    """La migración `0003` se revierte y se vuelve a aplicar sin errores."""
    cfg = _config()

    command.downgrade(cfg, "0002")
    command.upgrade(cfg, "head")


def test_migration_0003_downgrade_refuses_to_discard_incompatible_data() -> None:
    """Con dos alertas vivas del mismo (campo, evento), el downgrade falla con un mensaje claro."""
    phone_number = f"+54{uuid.uuid4().int % 10**12:012d}"

    async def _setup() -> None:
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                user_id = (
                    await conn.execute(
                        text(
                            "INSERT INTO users (phone_number, name)"
                            " VALUES (:phone, 'Downgrade probe') RETURNING id"
                        ),
                        {"phone": phone_number},
                    )
                ).scalar_one()
                field_id = (
                    await conn.execute(
                        text(
                            "INSERT INTO fields (user_id, name, latitude, longitude)"
                            " VALUES (:user_id, 'Probe', -34.6, -58.4) RETURNING id"
                        ),
                        {"user_id": user_id},
                    )
                ).scalar_one()
                await conn.execute(
                    text(
                        "INSERT INTO alerts (field_id, event_type, threshold) VALUES"
                        " (:field_id, 'frost', 50), (:field_id, 'frost', 80)"
                    ),
                    {"field_id": field_id},
                )
        finally:
            await engine.dispose()

    async def _cleanup() -> None:
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("DELETE FROM users WHERE phone_number = :phone"),
                    {"phone": phone_number},
                )
        finally:
            await engine.dispose()

    asyncio.run(_setup())
    try:
        with pytest.raises(RuntimeError, match="Downgrade de la migración 0003 bloqueado"):
            command.downgrade(_config(), "0002")
    finally:
        asyncio.run(_cleanup())
