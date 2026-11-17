"""Tests de la migración inicial (docs/TESTING.md §test_migrations).

El `upgrade`/`downgrade` de `0001` lo ejecuta la fixture de sesión de `conftest.py` antes de
cualquier test; acá se verifica que el esquema migrado y los modelos no divergen.
"""

import asyncio
import os
import uuid
from datetime import UTC, date, datetime
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


def test_migration_0005_upgrade_and_downgrade_are_clean() -> None:
    """La migración `0005` se revierte y se vuelve a aplicar sin errores."""
    cfg = _config()

    command.downgrade(cfg, "0004")
    command.upgrade(cfg, "head")


def test_migration_0006_upgrade_and_downgrade_are_clean() -> None:
    """La migración `0006` se revierte y se vuelve a aplicar sin errores."""
    cfg = _config()

    command.downgrade(cfg, "0005")
    command.upgrade(cfg, "head")


def test_migration_0007_upgrade_and_downgrade_are_clean() -> None:
    """La migración `0007` se revierte y se vuelve a aplicar sin errores."""
    cfg = _config()

    command.downgrade(cfg, "0006")
    command.upgrade(cfg, "head")


def test_migration_0008_upgrade_and_downgrade_are_clean() -> None:
    """La migración `0008` se revierte y se vuelve a aplicar sin errores (S12b)."""
    cfg = _config()

    command.downgrade(cfg, "0007")
    command.upgrade(cfg, "head")


def test_migration_0009_upgrade_and_downgrade_are_clean() -> None:
    """La migración `0009` se revierte y se vuelve a aplicar sin errores (S13).

    `downgrade()` elimina las tres columnas y los dos CHECK de `users` (DATA_MODEL.md
    §Migración 0009); el `upgrade` siguiente las recrea y el resto de la suite vuelve a
    correr sobre el esquema completo.
    """
    cfg = _config()

    command.downgrade(cfg, "0008")
    command.upgrade(cfg, "head")


def test_migration_0008_backfills_day_states_for_already_notified_days() -> None:
    """`0008`: un día ya notificado queda desarmado en episodio 1 (no se re-avisa)."""
    cfg = _config()
    phone_number = f"+54{uuid.uuid4().int % 10**12:012d}"
    first_day = date(2026, 11, 20)
    second_day = date(2026, 11, 21)
    cancelled_day = date(2026, 11, 22)

    async def _insert_legacy_rows() -> None:
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                user_id = (
                    await conn.execute(
                        text(
                            "INSERT INTO users (phone_number, name)"
                            " VALUES (:phone, 'Episodes backfill probe') RETURNING id"
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
                alert_id = (
                    await conn.execute(
                        text(
                            "INSERT INTO alerts (field_id, event_type, threshold)"
                            " VALUES (:field_id, 'frost', 70) RETURNING id"
                        ),
                        {"field_id": field_id},
                    )
                ).scalar_one()
                # Antes de `0008` la clave es (alert_id, forecast_date) y no hay `episode`.
                await conn.execute(
                    text(
                        "INSERT INTO notifications"
                        " (alert_id, forecast_date, event_type, probability, threshold, status)"
                        " VALUES (:alert_id, :first_day, 'frost', 90, 70, 'pending'),"
                        "        (:alert_id, :second_day, 'frost', 85, 70, 'pending'),"
                        "        (:alert_id, :cancelled_day, 'frost', 80, 70, 'cancelled')"
                    ),
                    {
                        "alert_id": alert_id,
                        "first_day": first_day,
                        "second_day": second_day,
                        "cancelled_day": cancelled_day,
                    },
                )
        finally:
            await engine.dispose()

    async def _day_states() -> list[tuple[date, int, bool]]:
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                rows = (
                    await conn.execute(
                        text(
                            "SELECT s.forecast_date, s.episode, s.armed"
                            " FROM alert_day_states s"
                            " JOIN alerts a ON a.id = s.alert_id"
                            " JOIN fields f ON f.id = a.field_id"
                            " JOIN users u ON u.id = f.user_id"
                            " WHERE u.phone_number = :phone"
                            " ORDER BY s.forecast_date"
                        ),
                        {"phone": phone_number},
                    )
                ).all()
                return [(row.forecast_date, row.episode, row.armed) for row in rows]
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

    command.downgrade(cfg, "0007")
    try:
        asyncio.run(_insert_legacy_rows())
        command.upgrade(cfg, "head")
        assert asyncio.run(_day_states()) == [
            (first_day, 1, False),
            (second_day, 1, False),
            # El backfill cubre también las notificaciones canceladas: el día existió.
            (cancelled_day, 1, False),
        ]
    finally:
        # La base tiene que quedar en `head` para el resto de la suite.
        command.upgrade(cfg, "head")
        asyncio.run(_cleanup())


def test_migration_0008_downgrade_refuses_to_discard_renotified_days() -> None:
    """Con alguna notificación de episodio > 1, el downgrade falla con un mensaje claro."""
    cfg = _config()
    phone_number = f"+54{uuid.uuid4().int % 10**12:012d}"

    async def _insert_episode_two() -> None:
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                user_id = (
                    await conn.execute(
                        text(
                            "INSERT INTO users (phone_number, name)"
                            " VALUES (:phone, 'Episodes downgrade probe') RETURNING id"
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
                alert_id = (
                    await conn.execute(
                        text(
                            "INSERT INTO alerts (field_id, event_type, threshold)"
                            " VALUES (:field_id, 'frost', 70) RETURNING id"
                        ),
                        {"field_id": field_id},
                    )
                ).scalar_one()
                await conn.execute(
                    text(
                        "INSERT INTO notifications"
                        " (alert_id, forecast_date, event_type, probability, threshold, episode)"
                        " VALUES (:alert_id, DATE '2026-11-20', 'frost', 90, 70, 1),"
                        "        (:alert_id, DATE '2026-11-20', 'frost', 95, 70, 2)"
                    ),
                    {"alert_id": alert_id},
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

    asyncio.run(_insert_episode_two())
    try:
        with pytest.raises(RuntimeError, match="Downgrade de la migración 0008 bloqueado"):
            command.downgrade(cfg, "0007")
    finally:
        asyncio.run(_cleanup())


def test_migration_0007_backfills_timezone_for_existing_rows_and_downgrade_drops_it() -> None:
    """`0007`: las filas previas toman el default; `downgrade()` elimina la columna (S12a)."""
    cfg = _config()
    phone_number = f"+54{uuid.uuid4().int % 10**12:012d}"
    default_timezone = "America/Argentina/Buenos_Aires"

    async def _insert_legacy_field() -> int:
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                user_id = (
                    await conn.execute(
                        text(
                            "INSERT INTO users (phone_number, name)"
                            " VALUES (:phone, 'Timezone probe') RETURNING id"
                        ),
                        {"phone": phone_number},
                    )
                ).scalar_one()
                # Sin `timezone`: la columna todavía no existe antes de `0007`.
                field_id = (
                    await conn.execute(
                        text(
                            "INSERT INTO fields (user_id, name, latitude, longitude)"
                            " VALUES (:user_id, 'Probe', -34.6, -58.4) RETURNING id"
                        ),
                        {"user_id": user_id},
                    )
                ).scalar_one()
                return int(field_id)
        finally:
            await engine.dispose()

    async def _timezone(field_id: int) -> str:
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                value = (
                    await conn.execute(
                        text("SELECT timezone FROM fields WHERE id = :field_id"),
                        {"field_id": field_id},
                    )
                ).scalar_one()
                assert isinstance(value, str)
                return value
        finally:
            await engine.dispose()

    async def _column_exists() -> bool:
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                count = (
                    await conn.execute(
                        text(
                            "SELECT count(*) FROM information_schema.columns"
                            " WHERE table_name = 'fields' AND column_name = 'timezone'"
                        )
                    )
                ).scalar_one()
                return bool(count)
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

    command.downgrade(cfg, "0006")
    try:
        field_id = asyncio.run(_insert_legacy_field())
        command.upgrade(cfg, "head")
        assert asyncio.run(_timezone(field_id)) == default_timezone
        command.downgrade(cfg, "0006")
        assert asyncio.run(_column_exists()) is False
    finally:
        # La base tiene que quedar en `head` para el resto de la suite.
        command.upgrade(cfg, "head")
        asyncio.run(_cleanup())


def test_migration_0006_removes_non_alert_keys_and_makes_user_id_not_null() -> None:
    """`0006`: borra las claves de users/fields y deja `user_id NOT NULL`."""
    cfg = _config()
    phone_number = f"+54{uuid.uuid4().int % 10**12:012d}"

    async def _insert_rows() -> int:
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                user_id = (
                    await conn.execute(
                        text(
                            "INSERT INTO users (phone_number, name)"
                            " VALUES (:phone, 'Idempotency probe') RETURNING id"
                        ),
                        {"phone": phone_number},
                    )
                ).scalar_one()
                await conn.execute(
                    text(
                        "INSERT INTO idempotency_keys"
                        " (user_id, scope, key, request_hash, status_code,"
                        "  response_body, expires_at)"
                        " VALUES"
                        " (:user_id, 'POST /api/v1/alerts', 'keep',"
                        "  repeat('a', 64), 201, '{}', now() + interval '1 day'),"
                        " (NULL, 'POST /api/v1/users', 'drop',"
                        "  repeat('b', 64), 201, '{}', now() + interval '1 day')"
                    ),
                    {"user_id": user_id},
                )
                return int(user_id)
        finally:
            await engine.dispose()

    async def _state() -> tuple[list[str], str | None]:
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                scopes = list(
                    (
                        await conn.execute(
                            text("SELECT scope FROM idempotency_keys WHERE key IN ('keep', 'drop')")
                        )
                    ).scalars()
                )
                nullable = (
                    await conn.execute(
                        text(
                            "SELECT is_nullable FROM information_schema.columns"
                            " WHERE table_name = 'idempotency_keys' AND column_name = 'user_id'"
                        )
                    )
                ).scalar_one()
                return scopes, str(nullable)
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
                await conn.execute(
                    text("DELETE FROM idempotency_keys WHERE key IN ('keep', 'drop')")
                )
        finally:
            await engine.dispose()

    command.downgrade(cfg, "0005")
    try:
        asyncio.run(_insert_rows())
        command.upgrade(cfg, "head")
        scopes, nullable = asyncio.run(_state())
        assert scopes == ["POST /api/v1/alerts"]
        assert nullable == "NO"
    finally:
        command.upgrade(cfg, "head")
        asyncio.run(_cleanup())


def test_migration_0005_backfills_issued_at_from_fetched_at() -> None:
    """Una fila que existía antes de `0005` queda con `issued_at = fetched_at` (nunca NULL)."""
    cfg = _config()
    phone_number = f"+54{uuid.uuid4().int % 10**12:012d}"
    fetched_at = datetime(2026, 11, 15, 9, 30, tzinfo=UTC)

    async def _insert_legacy_row() -> None:
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                user_id = (
                    await conn.execute(
                        text(
                            "INSERT INTO users (phone_number, name)"
                            " VALUES (:phone, 'Backfill probe') RETURNING id"
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
                        "INSERT INTO weather_forecasts"
                        " (field_id, forecast_date, event_type, probability, source, fetched_at)"
                        " VALUES (:field_id, DATE '2026-11-16', 'frost', 70, 'mock', :fetched_at)"
                    ),
                    {"field_id": field_id, "fetched_at": fetched_at},
                )
        finally:
            await engine.dispose()

    async def _issued_at() -> datetime:
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                value = (
                    await conn.execute(
                        text(
                            "SELECT wf.issued_at"
                            " FROM weather_forecasts wf"
                            " JOIN fields f ON f.id = wf.field_id"
                            " JOIN users u ON u.id = f.user_id"
                            " WHERE u.phone_number = :phone"
                        ),
                        {"phone": phone_number},
                    )
                ).scalar_one()
                assert isinstance(value, datetime)
                return value
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

    command.downgrade(cfg, "0004")
    try:
        asyncio.run(_insert_legacy_row())
        command.upgrade(cfg, "head")
        assert asyncio.run(_issued_at()) == fetched_at
    finally:
        # Si algo falló, la base queda igualmente en `head` para el resto de la suite.
        command.upgrade(cfg, "head")
        asyncio.run(_cleanup())


def test_migration_0005_downgrade_with_data_drops_the_column_and_keeps_the_rows() -> None:
    """Con filas cargadas, `downgrade("0004")` borra la columna sin perder los pronósticos.

    Al volver a `head`, la columna reaparece y se rellena con `fetched_at`
    (docs/DATA_MODEL.md §Migración 0005).
    """
    cfg = _config()
    phone_number = f"+54{uuid.uuid4().int % 10**12:012d}"
    issued_at = datetime(2026, 11, 9, 6, 0, tzinfo=UTC)
    fetched_at = datetime(2026, 11, 10, 8, 30, tzinfo=UTC)

    async def _insert_row() -> None:
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                user_id = (
                    await conn.execute(
                        text(
                            "INSERT INTO users (phone_number, name)"
                            " VALUES (:phone, 'Downgrade data probe') RETURNING id"
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
                        "INSERT INTO weather_forecasts"
                        " (field_id, forecast_date, event_type, probability, source,"
                        "  issued_at, fetched_at)"
                        " VALUES (:field_id, DATE '2026-11-16', 'frost', 70, 'mock',"
                        "         :issued_at, :fetched_at)"
                    ),
                    {"field_id": field_id, "issued_at": issued_at, "fetched_at": fetched_at},
                )
        finally:
            await engine.dispose()

    async def _column_and_rows() -> tuple[bool, int]:
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                column_exists = bool(
                    (
                        await conn.execute(
                            text(
                                "SELECT count(*) FROM information_schema.columns"
                                " WHERE table_name = 'weather_forecasts'"
                                "   AND column_name = 'issued_at'"
                            )
                        )
                    ).scalar_one()
                )
                rows = (
                    await conn.execute(
                        text(
                            "SELECT count(*) FROM weather_forecasts wf"
                            " JOIN fields f ON f.id = wf.field_id"
                            " JOIN users u ON u.id = f.user_id"
                            " WHERE u.phone_number = :phone"
                        ),
                        {"phone": phone_number},
                    )
                ).scalar_one()
                return column_exists, rows
        finally:
            await engine.dispose()

    async def _issued_at() -> datetime:
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                value = (
                    await conn.execute(
                        text(
                            "SELECT wf.issued_at FROM weather_forecasts wf"
                            " JOIN fields f ON f.id = wf.field_id"
                            " JOIN users u ON u.id = f.user_id"
                            " WHERE u.phone_number = :phone"
                        ),
                        {"phone": phone_number},
                    )
                ).scalar_one()
                assert isinstance(value, datetime)
                return value
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

    asyncio.run(_insert_row())
    try:
        command.downgrade(cfg, "0004")
        column_exists, rows = asyncio.run(_column_and_rows())
        assert column_exists is False
        assert rows == 1
    finally:
        # La base tiene que quedar en `head` para el resto de la suite.
        command.upgrade(cfg, "head")
    try:
        assert asyncio.run(_issued_at()) == fetched_at
    finally:
        asyncio.run(_cleanup())
