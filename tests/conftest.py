"""Infraestructura de tests. Ver docs/TESTING.md.

- Base real de Postgres (`TEST_DATABASE_URL`, por defecto `agrobot_test` del docker-compose).
- Una vez por sesión: `alembic downgrade base` + `alembic upgrade head` (valida migraciones).
- Cada test corre dentro de una transacción que se revierte al final; los `commit()`
  de services/jobs se convierten en SAVEPOINTs (`join_transaction_mode="create_savepoint"`).
- Factories (docs/TESTING.md §Factories): valores por defecto únicos por contador.
"""

import itertools
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.db import get_session
from app.main import create_app
from app.models import Alert, Field, User, WeatherForecast
from app.models.enums import EventType
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
def test_sessionmaker(db_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    """Sesiones propias para los jobs: sus `commit()` son SAVEPOINTs del test.

    El rollback final de la transacción del test sigue limpiando todo.
    """
    return async_sessionmaker(
        bind=db_session.bind,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )


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


# --- Factories (docs/TESTING.md §Factories) ---------------------------------

_seq = itertools.count(1)

MakeUser = Callable[..., Awaitable[User]]
MakeField = Callable[..., Awaitable[Field]]
MakeForecast = Callable[..., Awaitable[WeatherForecast]]
MakeAlert = Callable[..., Awaitable[Alert]]


def auth(user: User) -> dict[str, str]:
    """Headers de la autenticación simulada de la API (header `X-User-Id`)."""
    return {"X-User-Id": str(user.id)}


@pytest.fixture
def make_user(db_session: AsyncSession) -> MakeUser:
    async def _make(*, phone_number: str | None = None, name: str | None = None) -> User:
        n = next(_seq)
        user = User(phone_number=phone_number or f"+54911{n:08d}", name=name or f"User {n}")
        db_session.add(user)
        await db_session.flush()
        return user

    return _make


@pytest.fixture
def make_field(db_session: AsyncSession, make_user: MakeUser) -> MakeField:
    async def _make(
        user: User | None = None,
        *,
        name: str | None = None,
        latitude: Decimal = Decimal("-34.6"),
        longitude: Decimal = Decimal("-58.4"),
    ) -> Field:
        n = next(_seq)
        owner = user if user is not None else await make_user()
        field = Field(
            user_id=owner.id,
            name=name or f"Field {n}",
            latitude=latitude,
            longitude=longitude,
        )
        db_session.add(field)
        await db_session.flush()
        return field

    return _make


@pytest.fixture
def make_forecast(db_session: AsyncSession) -> MakeForecast:
    async def _make(
        field: Field,
        *,
        forecast_date: date,
        event_type: EventType,
        probability: int,
        source: str = "mock",
    ) -> WeatherForecast:
        forecast = WeatherForecast(
            field_id=field.id,
            forecast_date=forecast_date,
            event_type=event_type,
            probability=probability,
            source=source,
            fetched_at=datetime.now(UTC),
        )
        db_session.add(forecast)
        await db_session.flush()
        return forecast

    return _make


@pytest.fixture
def make_alert(db_session: AsyncSession) -> MakeAlert:
    async def _make(
        field: Field,
        *,
        event_type: EventType = EventType.FROST,
        threshold: int = 70,
        is_active: bool = True,
    ) -> Alert:
        alert = Alert(
            field_id=field.id,
            event_type=event_type,
            threshold=threshold,
            is_active=is_active,
        )
        db_session.add(alert)
        await db_session.flush()
        return alert

    return _make
