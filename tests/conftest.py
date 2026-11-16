"""Infraestructura de tests. Ver docs/TESTING.md.

- Base real de Postgres (`TEST_DATABASE_URL`, por defecto `agrobot_test` del docker-compose).
- Una vez por sesión: `alembic downgrade base` + `alembic upgrade head` (valida migraciones).
- Cada test corre dentro de una transacción que se revierte al final; los `commit()`
  de services/jobs se convierten en SAVEPOINTs (`join_transaction_mode="create_savepoint"`).

TODO(S1): agregar acá las factories descritas en docs/TESTING.md §Factories
(`make_user`, `make_field`, `make_forecast`, `make_alert`) una vez existan los modelos.
"""

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.db import get_session
from app.main import create_app
from app.notifiers.base import NotificationMessage, NotifierError

ROOT = Path(__file__).resolve().parent.parent
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/agrobot_test",
)


@pytest.fixture(scope="session", autouse=True)
def _migrated_database() -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


@pytest.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    async with engine.connect() as conn:
        outer = await conn.begin()
        session = AsyncSession(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        try:
            yield session
        finally:
            await session.close()
            await outer.rollback()
    await engine.dispose()


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    app = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


class FakeNotifier:
    """Notifier en memoria. `fail_times=n` hace fallar los primeros n envíos."""

    def __init__(self, *, fail_times: int = 0) -> None:
        self.sent: list[NotificationMessage] = []
        self.fail_times = fail_times
        self.calls = 0

    async def send(self, message: NotificationMessage) -> None:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise NotifierError("simulated failure")
        self.sent.append(message)


@pytest.fixture
def fake_notifier() -> FakeNotifier:
    return FakeNotifier()
