"""Tests de la migración inicial (docs/TESTING.md §test_migrations).

El `upgrade`/`downgrade` de `0001` lo ejecuta la fixture de sesión de `conftest.py` antes de
cualquier test; acá se verifica que el esquema migrado y los modelos no divergen.
"""

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

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
