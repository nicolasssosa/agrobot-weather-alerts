"""Datos demo: `python -m app.scripts.seed`.

Crea los usuarios, campos y alertas de docs/JOBS.md §Seed, corre la ingesta mock de 14 días
desde hoy (UTC) **solo sobre los 3 campos del seed** y commitea. Es idempotente: busca cada
fila por su clave natural antes de crearla, así que correrlo N veces no duplica ni falla.

Guarda de entorno: en `staging`/`production` sale con error sin tocar la base (S7 SEC-1).

Limitación: no es seguro ante dos corridas concurrentes (check-then-insert). Es un script de
demo one-shot; no se diseñó para ejecutarse en paralelo (S7 CR-1).
"""

import argparse
import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.db import get_engine, get_sessionmaker
from app.core.logging import configure_logging
from app.core.pii import mask_phone
from app.models import Alert, EventType, Field, User
from app.weather.ingestion import ingest_forecasts
from app.weather.provider import MockWeatherProvider

logger = logging.getLogger(__name__)

#: Horizonte de la ingesta mock del seed (docs/JOBS.md §Seed).
SEED_DAYS = 14

#: Entornos donde el seed no debe correr: pisaría datos reales (S7 SEC-1).
_UNSAFE_ENVIRONMENTS = frozenset({"staging", "production"})

#: Usuarios demo: (teléfono E.164, nombre).
_USER_SEEDS: tuple[tuple[str, str], ...] = (
    ("+5491100000001", "Ana Gómez"),
    ("+5491100000002", "Bruno Díaz"),
)

#: Campos demo: (teléfono del dueño, nombre, latitud, longitud).
_FIELD_SEEDS: tuple[tuple[str, str, Decimal, Decimal], ...] = (
    ("+5491100000001", "Lote Norte", Decimal("-34.60"), Decimal("-58.38")),
    ("+5491100000001", "Lote Sur", Decimal("-34.90"), Decimal("-58.50")),
    ("+5491100000002", "La Esperanza", Decimal("-31.42"), Decimal("-64.18")),
)

#: Alertas demo: (teléfono del dueño, nombre del campo, evento, umbral).
_ALERT_SEEDS: tuple[tuple[str, str, EventType, int], ...] = (
    ("+5491100000001", "Lote Norte", EventType.FROST, 60),
    ("+5491100000001", "Lote Norte", EventType.RAIN, 80),
    ("+5491100000001", "Lote Sur", EventType.HAIL, 50),
    ("+5491100000002", "La Esperanza", EventType.HEAT_WAVE, 40),
)


@dataclass(frozen=True, slots=True)
class SeedUser:
    """Usuario del seed, con sus datos para el resumen."""

    id: int
    name: str
    phone_number: str


@dataclass(frozen=True, slots=True)
class SeedField:
    """Campo del seed."""

    id: int
    user_id: int
    name: str


@dataclass(frozen=True, slots=True)
class SeedAlert:
    """Alerta del seed."""

    id: int
    field_id: int
    event_type: EventType
    threshold: int


@dataclass(frozen=True, slots=True)
class SeedSummary:
    """Entidades del seed y filas de pronóstico procesadas por la ingesta."""

    users: tuple[SeedUser, ...]
    fields: tuple[SeedField, ...]
    alerts: tuple[SeedAlert, ...]
    forecast_rows: int


def _ensure_environment_is_safe() -> None:
    """Aborta en `staging`/`production`: el seed es de demo y pisa pronósticos reales."""
    environment = get_settings().environment
    if environment in _UNSAFE_ENVIRONMENTS:
        raise SystemExit(
            f"seed abortado: environment={environment!r} no está permitido. "
            "Este script es solo para demo local/test."
        )


async def _get_or_create_user(session: AsyncSession, *, phone_number: str, name: str) -> User:
    """Busca el usuario por teléfono (UNIQUE) y lo crea si no existe."""
    user = await session.scalar(select(User).where(User.phone_number == phone_number))
    if user is None:
        user = User(phone_number=phone_number, name=name)
        session.add(user)
        await session.flush()
    return user


async def _get_or_create_field(
    session: AsyncSession, *, user: User, name: str, latitude: Decimal, longitude: Decimal
) -> Field:
    """Busca el campo por `(user_id, name)` (UNIQUE) y lo crea si no existe."""
    field = await session.scalar(select(Field).where(Field.user_id == user.id, Field.name == name))
    if field is None:
        field = Field(
            user_id=user.id,
            name=name,
            latitude=latitude,
            longitude=longitude,
        )
        session.add(field)
        await session.flush()
    return field


async def _get_or_create_alert(
    session: AsyncSession, *, field: Field, event_type: EventType, threshold: int
) -> Alert:
    """Busca la alerta por `(field_id, event_type)` (UNIQUE) y la crea si no existe."""
    alert = await session.scalar(
        select(Alert).where(Alert.field_id == field.id, Alert.event_type == event_type)
    )
    if alert is None:
        alert = Alert(field_id=field.id, event_type=event_type, threshold=threshold)
        session.add(alert)
        await session.flush()
    return alert


async def seed(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
    days: int = SEED_DAYS,
) -> SeedSummary:
    """Crea (o reutiliza) los datos demo, ingesta pronósticos y commitea.

    `now` fija el día de inicio de la ingesta (en producción, hoy UTC). No commitea antes de
    terminar, así una falla de la ingesta no deja medios datos sueltos: el seed es una sola
    transacción. La ingesta mock se limita a los campos del seed, así no pisa pronósticos de
    otros campos (S7 SEC-1). No es seguro ante dos corridas concurrentes (S7 CR-1).
    """
    _ensure_environment_is_safe()
    start = (now if now is not None else datetime.now(UTC)).date()
    async with sessionmaker() as session:
        users_by_phone: dict[str, User] = {}
        for phone_number, name in _USER_SEEDS:
            users_by_phone[phone_number] = await _get_or_create_user(
                session, phone_number=phone_number, name=name
            )

        fields_by_key: dict[tuple[int, str], Field] = {}
        for phone_number, name, latitude, longitude in _FIELD_SEEDS:
            owner = users_by_phone[phone_number]
            fields_by_key[(owner.id, name)] = await _get_or_create_field(
                session,
                user=owner,
                name=name,
                latitude=latitude,
                longitude=longitude,
            )

        alerts: list[Alert] = []
        for phone_number, field_name, event_type, threshold in _ALERT_SEEDS:
            owner = users_by_phone[phone_number]
            field = fields_by_key[(owner.id, field_name)]
            alerts.append(
                await _get_or_create_alert(
                    session, field=field, event_type=event_type, threshold=threshold
                )
            )

        forecast_rows = await ingest_forecasts(
            session,
            MockWeatherProvider(),
            start=start,
            days=days,
            field_ids=[field.id for field in fields_by_key.values()],
        )
        await session.commit()

    summary = SeedSummary(
        users=tuple(
            SeedUser(id=user.id, name=user.name, phone_number=user.phone_number)
            for user in users_by_phone.values()
        ),
        fields=tuple(
            SeedField(id=field.id, user_id=field.user_id, name=field.name)
            for field in fields_by_key.values()
        ),
        alerts=tuple(
            SeedAlert(
                id=alert.id,
                field_id=alert.field_id,
                event_type=alert.event_type,
                threshold=alert.threshold,
            )
            for alert in alerts
        ),
        forecast_rows=forecast_rows,
    )
    logger.info(
        "seed_done users=%d fields=%d alerts=%d forecasts=%d",
        len(summary.users),
        len(summary.fields),
        len(summary.alerts),
        summary.forecast_rows,
    )
    return summary


def _print_summary(summary: SeedSummary) -> None:
    """Imprime ids, filas ingestadas y un ejemplo de curl (sin teléfonos completos)."""
    print(
        f"seed_done users={len(summary.users)} fields={len(summary.fields)} "
        f"alerts={len(summary.alerts)} forecasts={summary.forecast_rows}"
    )
    for user in summary.users:
        print(f"  user id={user.id} name={user.name!r} phone={mask_phone(user.phone_number)}")
    for field in summary.fields:
        print(f"  field id={field.id} user_id={field.user_id} name={field.name!r}")
    for alert in summary.alerts:
        print(
            f"  alert id={alert.id} field_id={alert.field_id} "
            f"event={alert.event_type.value} threshold={alert.threshold}"
        )
    if summary.users and summary.fields:
        print(
            "ejemplo:\n"
            f'  curl -s -H "X-User-Id: {summary.users[0].id}" '
            f"http://localhost:8000/api/v1/fields/{summary.fields[0].id}/forecasts"
        )


async def _run(now: datetime | None = None) -> SeedSummary:
    """Ejecuta el seed y libera el engine del proceso."""
    try:
        return await seed(get_sessionmaker(), now=now)
    finally:
        await get_engine().dispose()
        # Igual que ingest.py: un segundo `main()` en el mismo proceso no debe reusar el pool
        # de un engine atado a un event loop ya cerrado.
        get_engine.cache_clear()
        get_sessionmaker.cache_clear()


def main(argv: Sequence[str] | None = None) -> None:
    """Punto de entrada del script."""
    configure_logging()
    parser = argparse.ArgumentParser(description="Crea los datos demo y los pronósticos mock")
    parser.parse_args(argv)
    _print_summary(asyncio.run(_run()))


if __name__ == "__main__":
    main()
