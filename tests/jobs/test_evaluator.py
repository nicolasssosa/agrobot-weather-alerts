"""Tests del evaluador de alertas.

Ver docs/TESTING.md §`tests/jobs/test_evaluator.py` y docs/JOBS.md §Evaluador.
Todas las corridas usan un `today` fijo: nunca el reloj real.
"""

import asyncio
import contextlib
import logging
import random
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.jobs.episodes import INITIAL_STATE, DayState, reset, step
from app.jobs.evaluator import (
    DEFAULT_LOCK_WAIT_MS,
    EVALUATOR_LOCK_KEY,
    _build_statement,
    _take_evaluation_lock,
    evaluate_alerts,
)
from app.models import (
    Alert,
    AlertDayState,
    EventType,
    Field,
    Notification,
    NotificationStatus,
    User,
    WeatherForecast,
)
from app.schemas.alert import AlertUpdate
from app.services.alerts import update_alert
from tests.conftest import (
    TEST_DATABASE_URL,
    MakeAlert,
    MakeField,
    MakeForecast,
    MakeUser,
    auth,
)

TODAY = date(2026, 11, 16)
#: Timestamp fijo para metadatos (`fetched_at`, `issued_at`); nunca el reloj real.
FIXED_AT = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
#: Corrida del evaluador, fija (S11).
NOW = FIXED_AT
#: Ventana de frescura por defecto de los tests (docs/JOBS.md §Evaluador).
MAX_AGE_HOURS = 24
#: Tolerancia de reloj para un `issued_at` futuro (docs/JOBS.md §Ingesta mock).
FUTURE_TOLERANCE_MINUTES = 60
#: Margen de histéresis por defecto (D1): dispara en >= 70, re-arma en < 60.
REARM_MARGIN = 10
#: Path del recurso de alertas (para los tests de PATCH a nivel HTTP).
ALERTS_API = "/api/v1/alerts"


def _evaluate(
    session: AsyncSession,
    *,
    now: datetime = NOW,
    horizon_days: int = 0,
    max_forecast_age_hours: int = MAX_AGE_HOURS,
    future_tolerance_minutes: int = FUTURE_TOLERANCE_MINUTES,
    rearm_margin: int = REARM_MARGIN,
    lock_wait_ms: int = DEFAULT_LOCK_WAIT_MS,
) -> Awaitable[int]:
    """Corre el evaluador con `now`/ventana fijos (nunca el reloj real).

    `horizon_days=0` reproduce el comportamiento previo a S12a (sin horizonte); los tests del
    horizonte lo pasan explícito. `lock_wait_ms` corto en los tests de locks de S14a.
    """
    return evaluate_alerts(
        session,
        now=now,
        horizon_days=horizon_days,
        max_forecast_age_hours=max_forecast_age_hours,
        future_tolerance_minutes=future_tolerance_minutes,
        rearm_margin=rearm_margin,
        lock_wait_ms=lock_wait_ms,
    )


async def _notifications(
    session: AsyncSession, *, alert_id: int | None = None
) -> list[Notification]:
    """Notificaciones ordenadas; opcionalmente las de una alerta."""
    statement = select(Notification).order_by(
        Notification.alert_id, Notification.forecast_date, Notification.episode
    )
    if alert_id is not None:
        statement = statement.where(Notification.alert_id == alert_id)
    result = await session.execute(statement)
    return list(result.scalars().all())


def _compiled_sql(*, literal_binds: bool = False) -> str:
    """SQL que Postgres recibe para una corrida (dialecto postgresql)."""
    options = {"literal_binds": True} if literal_binds else {}
    return str(
        _build_statement(
            now=NOW,
            horizon_days=0,
            max_forecast_age_hours=MAX_AGE_HOURS,
            future_tolerance_minutes=FUTURE_TOLERANCE_MINUTES,
        ).compile(dialect=postgresql.dialect(), compile_kwargs=options)
    )


async def _day_states(session: AsyncSession) -> list[AlertDayState]:
    """Estados de episodio ordenados por alerta y día."""
    result = await session.execute(
        select(AlertDayState).order_by(AlertDayState.alert_id, AlertDayState.forecast_date)
    )
    return list(result.scalars().all())


async def _day_state(session: AsyncSession, alert_id: int, forecast_date: date) -> DayState:
    """Estado de episodio de un (alerta, día); sin fila = `INITIAL_STATE`."""
    row = (
        await session.execute(
            select(AlertDayState.episode, AlertDayState.armed).where(
                AlertDayState.alert_id == alert_id,
                AlertDayState.forecast_date == forecast_date,
            )
        )
    ).one_or_none()
    return INITIAL_STATE if row is None else DayState(episode=row.episode, armed=row.armed)


async def _stored_day_state(
    session_factory: async_sessionmaker[AsyncSession], alert_id: int
) -> tuple[int, bool]:
    """`(episode, armed)` del único día de la alerta, en una conexión nueva y commiteada."""
    async with session_factory() as session:
        row = (
            await session.execute(
                select(AlertDayState.episode, AlertDayState.armed).where(
                    AlertDayState.alert_id == alert_id
                )
            )
        ).one()
    return int(row.episode), bool(row.armed)


# --- Forma del SQL (CR-1, CR-2) ------------------------------------------


def test_active_filter_is_a_bare_boolean_and_excludes_deleted_alerts() -> None:
    """El predicado matchea el índice parcial `... WHERE is_active AND deleted_at IS NULL`.

    `WHERE alerts.is_active` (no `IS true`) es lo que habilita el índice; `deleted_at IS NULL`
    excluye las alertas borradas lógicamente (S9).
    """
    sql = _compiled_sql()

    assert "alerts.is_active" in sql
    assert "alerts.deleted_at IS NULL" in sql
    assert "IS TRUE" not in sql.upper()


def test_insert_is_counted_in_sql_without_materializing_rows() -> None:
    """El disparo va en CTEs y el conteo lo hace Postgres: nunca se traen las filas a Python.

    `fired` hace el upsert del estado con `DO UPDATE ... WHERE armed` (sigue arriba = no
    dispara) y `ins` inserta la notificación con `ON CONFLICT (alert_id, forecast_date,
    episode) DO NOTHING` (red de seguridad de idempotencia).
    """
    sql = _compiled_sql()

    assert sql.startswith("WITH candidatos AS")
    assert "fired AS" in sql
    assert "INSERT INTO alert_day_states" in sql
    assert "ON CONFLICT (alert_id, forecast_date) DO UPDATE" in sql
    assert "WHERE alert_day_states.armed" in sql
    assert sql.count("INSERT INTO notifications") == 1
    assert "ON CONFLICT (alert_id, forecast_date, episode) DO NOTHING" in sql
    assert "RETURNING notifications.id" in sql
    assert "count(*)" in sql
    assert "FROM ins" in sql


async def test_planner_uses_the_partial_index(
    db_session: AsyncSession,
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """Con `enable_seqscan = off`, el plan del evaluador elige el índice parcial.

    Crea una sola alerta activa entre muchas inactivas para que el índice parcial sea
    claramente más barato. `SET LOCAL` y `ANALYZE` viven en la transacción del test, que
    se revierte al final.
    """
    user = await make_user()
    fields = [await make_field(user, name=f"Lote plan {index}") for index in range(9)]
    for field in fields[:8]:
        for event_type in EventType:
            await make_alert(field, event_type=event_type, threshold=50, is_active=False)
    # Alertas borradas: tampoco pertenecen al índice parcial (S9).
    for event_type in EventType:
        deleted = await make_alert(fields[0], event_type=event_type, threshold=60)
        deleted.deleted_at = FIXED_AT
    await db_session.flush()
    active_field = fields[8]
    await make_alert(active_field, event_type=EventType.FROST, threshold=50)
    await make_forecast(
        active_field, forecast_date=TODAY, event_type=EventType.FROST, probability=80
    )

    await db_session.execute(text("ANALYZE alerts"))
    await db_session.execute(text("ANALYZE weather_forecasts"))
    await db_session.execute(text("SET LOCAL enable_seqscan = off"))
    plan_lines = (
        await db_session.execute(text("EXPLAIN " + _compiled_sql(literal_binds=True)))
    ).scalars()
    plan = "\n".join(plan_lines)

    assert "ix_alerts_active_field_event" in plan


async def test_probability_equal_to_threshold_creates_notification(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    field = await make_field()
    alert = await make_alert(field, event_type=EventType.FROST, threshold=70)
    await make_forecast(field, forecast_date=TODAY, event_type=EventType.FROST, probability=70)

    created = await _evaluate(db_session)

    assert created == 1
    [notification] = await _notifications(db_session)
    assert notification.alert_id == alert.id
    assert notification.forecast_date == TODAY
    # Snapshot de los valores que dispararon la alerta.
    assert notification.event_type == EventType.FROST
    assert notification.probability == 70
    assert notification.threshold == 70
    assert notification.status == NotificationStatus.PENDING
    assert notification.attempts == 0
    assert notification.episode == 1
    assert notification.next_attempt_at is not None
    assert notification.sent_at is None
    assert notification.last_error is None


async def test_probability_below_threshold_does_not_create_notification(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    field = await make_field()
    await make_alert(field, event_type=EventType.FROST, threshold=70)
    await make_forecast(field, forecast_date=TODAY, event_type=EventType.FROST, probability=69)

    created = await _evaluate(db_session)

    assert created == 0
    assert await _notifications(db_session) == []


async def test_inactive_alert_does_not_create_notification(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    field = await make_field()
    await make_alert(field, event_type=EventType.FROST, threshold=70, is_active=False)
    await make_forecast(field, forecast_date=TODAY, event_type=EventType.FROST, probability=100)

    created = await _evaluate(db_session)

    assert created == 0
    assert await _notifications(db_session) == []


async def test_soft_deleted_alert_does_not_create_notification(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """S9: una alerta borrada lógicamente no se evalúa aunque siga `is_active`."""
    field = await make_field()
    alert = await make_alert(field, event_type=EventType.FROST, threshold=70)
    alert.deleted_at = FIXED_AT
    await db_session.flush()
    await make_forecast(field, forecast_date=TODAY, event_type=EventType.FROST, probability=100)

    created = await _evaluate(db_session)

    assert created == 0
    assert await _notifications(db_session) == []


async def test_yesterday_forecast_does_not_create_notification(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    field = await make_field()
    await make_alert(field, event_type=EventType.FROST, threshold=70)
    await make_forecast(
        field,
        forecast_date=TODAY - timedelta(days=1),
        event_type=EventType.FROST,
        probability=100,
    )

    created = await _evaluate(db_session)

    assert created == 0
    assert await _notifications(db_session) == []


async def test_today_forecast_creates_notification(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    field = await make_field()
    await make_alert(field, event_type=EventType.FROST, threshold=70)
    await make_forecast(field, forecast_date=TODAY, event_type=EventType.FROST, probability=100)

    created = await _evaluate(db_session)

    assert created == 1
    [notification] = await _notifications(db_session)
    assert notification.forecast_date == TODAY


async def test_far_future_forecast_creates_notification(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """Sin horizonte (D2): todo día futuro cuenta, por lejano que sea."""
    field = await make_field()
    far = TODAY + timedelta(days=30)
    await make_alert(field, event_type=EventType.HEAT_WAVE, threshold=40)
    await make_forecast(field, forecast_date=far, event_type=EventType.HEAT_WAVE, probability=55)

    created = await _evaluate(db_session)

    assert created == 1
    [notification] = await _notifications(db_session)
    assert notification.forecast_date == far


async def test_only_same_field_and_same_event_type_match(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    field = await make_field()
    other_field = await make_field()
    await make_alert(field, event_type=EventType.FROST, threshold=70)
    # Mismo campo, otro evento: no tiene alerta.
    await make_forecast(field, forecast_date=TODAY, event_type=EventType.RAIN, probability=100)
    # Mismo evento, otro campo: no tiene alerta.
    await make_forecast(
        other_field, forecast_date=TODAY, event_type=EventType.FROST, probability=100
    )

    created = await _evaluate(db_session)

    assert created == 0
    assert await _notifications(db_session) == []


async def test_running_twice_does_not_duplicate(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    field = await make_field()
    await make_alert(field, event_type=EventType.FROST, threshold=70)
    await make_forecast(field, forecast_date=TODAY, event_type=EventType.FROST, probability=90)

    first = await _evaluate(db_session)
    second = await _evaluate(db_session)

    assert (first, second) == (1, 0)
    assert len(await _notifications(db_session)) == 1


async def test_forecast_staying_up_after_a_notification_does_not_renotify(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """D1: mientras el día sigue arriba (`armed = false`) una nueva corrida no re-avisa."""
    field = await make_field()
    alert = await make_alert(field, event_type=EventType.FROST, threshold=70)
    forecast = await make_forecast(
        field, forecast_date=TODAY, event_type=EventType.FROST, probability=80
    )

    assert await _evaluate(db_session) == 1

    forecast.probability = 95  # el pronóstico sube (upsert de la ingesta)
    await db_session.flush()
    assert await _evaluate(db_session) == 0

    [notification] = await _notifications(db_session)
    # El snapshot conserva lo que disparó la alerta, no el pronóstico vigente.
    assert notification.probability == 80
    assert notification.threshold == 70
    assert notification.episode == 1
    assert await _day_state(db_session, alert.id, TODAY) == DayState(episode=1, armed=False)


async def test_crossing_down_and_back_up_creates_the_next_episode(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """D1: arriba (ep. 1) -> debajo de `rearm_level` -> arriba crea el episodio 2."""
    field = await make_field()
    alert = await make_alert(field, event_type=EventType.FROST, threshold=70)
    forecast = await make_forecast(
        field, forecast_date=TODAY, event_type=EventType.FROST, probability=80
    )

    assert await _evaluate(db_session) == 1

    forecast.probability = 59  # < 70 - 10: re-arma
    await db_session.flush()
    assert await _evaluate(db_session) == 0
    assert await _day_state(db_session, alert.id, TODAY) == DayState(episode=1, armed=True)

    forecast.probability = 75  # vuelve a cruzar el umbral
    await db_session.flush()
    assert await _evaluate(db_session) == 1

    notifications = await _notifications(db_session, alert_id=alert.id)
    assert [(n.forecast_date, n.episode, n.probability) for n in notifications] == [
        (TODAY, 1, 80),
        (TODAY, 2, 75),
    ]
    assert await _day_state(db_session, alert.id, TODAY) == DayState(episode=2, armed=False)

    # Re-ejecutar el ciclo no duplica el episodio 2.
    assert await _evaluate(db_session) == 0
    assert len(await _notifications(db_session, alert_id=alert.id)) == 2


async def test_probability_inside_the_hysteresis_band_does_not_renotify(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """Con umbral 70 y margen 10, oscilar entre 60 y 69 no re-arma ni dispara."""
    field = await make_field()
    alert = await make_alert(field, event_type=EventType.FROST, threshold=70)
    forecast = await make_forecast(
        field, forecast_date=TODAY, event_type=EventType.FROST, probability=80
    )
    assert await _evaluate(db_session) == 1

    for probability in (69, 60, 65, 69, 60):
        forecast.probability = probability
        await db_session.flush()
        assert await _evaluate(db_session) == 0
        assert await _day_state(db_session, alert.id, TODAY) == DayState(episode=1, armed=False)

    forecast.probability = 70
    await db_session.flush()
    assert await _evaluate(db_session) == 0
    assert len(await _notifications(db_session, alert_id=alert.id)) == 1


async def test_the_rearm_level_scales_with_the_configured_margin(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """`rearm_margin=0`: bajar a `threshold - 1` ya re-arma (no hay banda)."""
    field = await make_field()
    alert = await make_alert(field, event_type=EventType.FROST, threshold=70)
    forecast = await make_forecast(
        field, forecast_date=TODAY, event_type=EventType.FROST, probability=80
    )
    assert await _evaluate(db_session, rearm_margin=0) == 1

    forecast.probability = 69
    await db_session.flush()
    assert await _evaluate(db_session, rearm_margin=0) == 0
    assert await _day_state(db_session, alert.id, TODAY) == DayState(episode=1, armed=True)

    forecast.probability = 70
    await db_session.flush()
    assert await _evaluate(db_session, rearm_margin=0) == 1
    assert await _day_state(db_session, alert.id, TODAY) == DayState(episode=2, armed=False)


async def test_the_rearm_level_never_goes_below_one(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """Umbral 5 y margen 99: `rearm_level` es 1, así que 0 re-arma y 1 dispara."""
    field = await make_field()
    alert = await make_alert(field, event_type=EventType.FROST, threshold=5)
    forecast = await make_forecast(
        field, forecast_date=TODAY, event_type=EventType.FROST, probability=5
    )
    assert await _evaluate(db_session, rearm_margin=99) == 1

    forecast.probability = 1  # 1 >= rearm_level (1): sigue desarmado
    await db_session.flush()
    assert await _evaluate(db_session, rearm_margin=99) == 0
    assert await _day_state(db_session, alert.id, TODAY) == DayState(episode=1, armed=False)

    forecast.probability = 0  # 0 < 1: re-arma
    await db_session.flush()
    assert await _evaluate(db_session, rearm_margin=99) == 0
    assert await _day_state(db_session, alert.id, TODAY) == DayState(episode=1, armed=True)


async def test_a_stale_forecast_does_not_rearm_a_fired_day(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """Regla 7 + D1: un pronóstico viejo no está en los candidatos, así que no re-arma."""
    field = await make_field()
    alert = await make_alert(field, event_type=EventType.FROST, threshold=70)
    forecast = await make_forecast(
        field,
        forecast_date=TODAY,
        event_type=EventType.FROST,
        probability=90,
        issued_at=NOW,
    )
    assert await _evaluate(db_session) == 1

    forecast.probability = 0
    forecast.issued_at = NOW - timedelta(hours=MAX_AGE_HOURS + 1)
    await db_session.flush()
    assert await _evaluate(db_session) == 0
    assert await _day_state(db_session, alert.id, TODAY) == DayState(episode=1, armed=False)

    # Cuando la ingesta vuelve a traer un pronóstico fresco y bajo, sí re-arma.
    forecast.probability = 30
    forecast.issued_at = NOW
    await db_session.flush()
    assert await _evaluate(db_session) == 0
    assert await _day_state(db_session, alert.id, TODAY) == DayState(episode=1, armed=True)


async def test_an_inactive_alert_does_not_rearm_its_days(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """Mientras la alerta está pausada el evaluador no la mira: el estado no cambia."""
    field = await make_field()
    alert = await make_alert(field, event_type=EventType.FROST, threshold=70)
    forecast = await make_forecast(
        field, forecast_date=TODAY, event_type=EventType.FROST, probability=90
    )
    assert await _evaluate(db_session) == 1

    alert.is_active = False
    forecast.probability = 0
    await db_session.flush()
    assert await _evaluate(db_session) == 0
    assert await _day_state(db_session, alert.id, TODAY) == DayState(episode=1, armed=False)


async def test_with_threshold_one_the_day_rearms_only_at_zero(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """Umbral 1: `rearm_level` es 1, así que la banda va de 1 a `threshold - 1` = vacía.

    Un día disparado solo se re-arma con probabilidad 0; con 1 sigue desarmado.
    """
    field = await make_field()
    alert = await make_alert(field, event_type=EventType.FROST, threshold=1)
    forecast = await make_forecast(
        field, forecast_date=TODAY, event_type=EventType.FROST, probability=1
    )
    assert await _evaluate(db_session) == 1
    assert await _day_state(db_session, alert.id, TODAY) == DayState(episode=1, armed=False)

    # Borde exacto: `probability == rearm_level` (1) no re-arma.
    assert await _evaluate(db_session) == 0
    assert await _day_state(db_session, alert.id, TODAY) == DayState(episode=1, armed=False)

    forecast.probability = 0
    await db_session.flush()
    assert await _evaluate(db_session) == 0
    assert await _day_state(db_session, alert.id, TODAY) == DayState(episode=1, armed=True)

    forecast.probability = 1
    await db_session.flush()
    assert await _evaluate(db_session) == 1
    assert await _day_state(db_session, alert.id, TODAY) == DayState(episode=2, armed=False)


async def test_a_day_outside_the_horizon_does_not_rearm(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """D1 + D2: si el día sale de la ventana no es candidato, así que no re-arma ni dispara."""
    field = await make_field()
    alert = await make_alert(field, event_type=EventType.FROST, threshold=70)
    in_horizon = await make_forecast(
        field, forecast_date=TODAY + timedelta(days=1), event_type=EventType.FROST, probability=90
    )
    far_day = TODAY + timedelta(days=5)
    out_of_horizon = await make_forecast(
        field, forecast_date=far_day, event_type=EventType.FROST, probability=90
    )
    # Sin límite superior (`horizon_days=0`) entran los dos días.
    assert await _evaluate(db_session, horizon_days=0) == 2

    in_horizon.probability = 0
    out_of_horizon.probability = 0
    await db_session.flush()
    # Con horizonte 3 solo el día +1 está en la ventana: el lejano conserva su estado.
    assert await _evaluate(db_session, horizon_days=3) == 0
    assert await _day_state(db_session, alert.id, TODAY + timedelta(days=1)) == DayState(
        episode=1, armed=True
    )
    assert await _day_state(db_session, alert.id, far_day) == DayState(episode=1, armed=False)


async def test_several_thresholds_of_the_same_field_and_event_keep_independent_states(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """D1/D5: dos alertas (30 y 90) del mismo campo y evento no comparten estado de episodio."""
    field = await make_field()
    warning = await make_alert(field, event_type=EventType.FROST, threshold=30)
    critical = await make_alert(field, event_type=EventType.FROST, threshold=90)
    forecast = await make_forecast(
        field, forecast_date=TODAY, event_type=EventType.FROST, probability=70
    )

    assert await _evaluate(db_session) == 1
    assert await _day_state(db_session, warning.id, TODAY) == DayState(episode=1, armed=False)
    # La alerta crítica nunca disparó: sin fila = armada, episodio 0.
    assert await _day_state(db_session, critical.id, TODAY) == INITIAL_STATE

    forecast.probability = 95  # la de 30 sigue desarmada; la de 90 cruza por primera vez
    await db_session.flush()
    assert await _evaluate(db_session) == 1
    assert await _day_state(db_session, warning.id, TODAY) == DayState(episode=1, armed=False)
    assert await _day_state(db_session, critical.id, TODAY) == DayState(episode=1, armed=False)

    forecast.probability = 10  # 10 < max(30-10, 1) y < max(90-10, 1): las dos se re-arman
    await db_session.flush()
    assert await _evaluate(db_session) == 0
    assert await _day_state(db_session, warning.id, TODAY) == DayState(episode=1, armed=True)
    assert await _day_state(db_session, critical.id, TODAY) == DayState(episode=1, armed=True)

    forecast.probability = 40  # solo la de 30 vuelve a cruzar
    await db_session.flush()
    assert await _evaluate(db_session) == 1
    assert await _day_state(db_session, warning.id, TODAY) == DayState(episode=2, armed=False)
    assert await _day_state(db_session, critical.id, TODAY) == DayState(episode=1, armed=True)

    notifications = await _notifications(db_session)
    assert [(n.alert_id, n.episode) for n in notifications] == [
        (warning.id, 1),
        (warning.id, 2),
        (critical.id, 1),
    ]


async def test_the_log_reports_created_and_rearmed_counts(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Log exacto `alerts_evaluated created=<n> rearmed=<m>` (JOBS.md §Evaluador)."""
    caplog.set_level(logging.INFO, logger="app.jobs.evaluator")
    field = await make_field()
    await make_alert(field, event_type=EventType.FROST, threshold=70)
    await make_alert(field, event_type=EventType.RAIN, threshold=70)
    frost = await make_forecast(
        field, forecast_date=TODAY, event_type=EventType.FROST, probability=90
    )
    await make_forecast(field, forecast_date=TODAY, event_type=EventType.RAIN, probability=90)

    assert await _evaluate(db_session) == 2
    assert any(
        record.getMessage().startswith("alerts_evaluated created=2 rearmed=0")
        for record in caplog.records
    )

    frost.probability = 0
    await db_session.flush()
    caplog.clear()
    assert await _evaluate(db_session) == 0
    assert any(
        record.getMessage().startswith("alerts_evaluated created=0 rearmed=1")
        for record in caplog.records
    )


async def test_multiple_alerts_and_days_are_counted_exactly(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    field = await make_field()
    frost = await make_alert(field, event_type=EventType.FROST, threshold=60)
    rain = await make_alert(field, event_type=EventType.RAIN, threshold=80)
    expected: set[tuple[int, date]] = set()
    for offset in range(3):
        day = TODAY + timedelta(days=offset)
        await make_forecast(
            field, forecast_date=day, event_type=EventType.FROST, probability=60 + offset
        )
        await make_forecast(
            field, forecast_date=day, event_type=EventType.RAIN, probability=80 + offset
        )
        expected.add((frost.id, day))
        expected.add((rain.id, day))
    # Evento sin alerta: no suma.
    await make_forecast(field, forecast_date=TODAY, event_type=EventType.HAIL, probability=100)
    # Bajo el umbral: no suma.
    await make_forecast(
        field,
        forecast_date=TODAY + timedelta(days=3),
        event_type=EventType.FROST,
        probability=59,
    )

    created = await _evaluate(db_session)

    assert created == 6
    notifications = await _notifications(db_session)
    assert len(notifications) == 6
    assert {(n.alert_id, n.forecast_date) for n in notifications} == expected


async def test_notifications_are_linked_to_the_alert_of_their_own_user(
    db_session: AsyncSession,
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    ana = await make_user(name="Ana")
    bruno = await make_user(name="Bruno")
    ana_field = await make_field(ana, name="Lote Norte")
    bruno_field = await make_field(bruno, name="La Esperanza")
    ana_alert = await make_alert(ana_field, event_type=EventType.FROST, threshold=60)
    bruno_alert = await make_alert(bruno_field, event_type=EventType.FROST, threshold=80)
    await make_forecast(ana_field, forecast_date=TODAY, event_type=EventType.FROST, probability=75)
    await make_forecast(
        bruno_field, forecast_date=TODAY, event_type=EventType.FROST, probability=95
    )

    created = await _evaluate(db_session)

    assert created == 2
    by_alert = {n.alert_id: n for n in await _notifications(db_session)}
    assert set(by_alert) == {ana_alert.id, bruno_alert.id}
    assert by_alert[ana_alert.id].probability == 75
    assert by_alert[ana_alert.id].threshold == 60
    assert by_alert[bruno_alert.id].probability == 95
    assert by_alert[bruno_alert.id].threshold == 80
    # Cada alerta pertenece al campo de su dueño: la notificación no cruza usuarios.
    alerts = {alert.id: alert for alert in (await db_session.scalars(select(Alert))).all()}
    assert alerts[ana_alert.id].field_id == ana_field.id
    assert alerts[bruno_alert.id].field_id == bruno_field.id


async def test_does_not_commit(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    field = await make_field()
    await make_alert(field, event_type=EventType.FROST, threshold=70)
    await make_forecast(field, forecast_date=TODAY, event_type=EventType.FROST, probability=90)

    assert await _evaluate(db_session) == 1
    assert len(await _notifications(db_session)) == 1

    # Si `evaluate_alerts` hubiera commiteado, las filas sobrevivirían a este rollback
    # (el commit del test se convierte en un SAVEPOINT ya liberado).
    await db_session.rollback()
    total = await db_session.scalar(select(func.count()).select_from(Notification))
    assert total == 0


# --- Bordes de los límites y de la base vacía ------------------------------


async def test_upper_bounds_fire(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """threshold=100 con probability=100 dispara (`>=` en el límite superior, D4)."""
    field = await make_field()
    alert = await make_alert(field, event_type=EventType.FROST, threshold=100)
    await make_forecast(field, forecast_date=TODAY, event_type=EventType.FROST, probability=100)

    created = await _evaluate(db_session)

    assert created == 1
    [notification] = await _notifications(db_session)
    assert (notification.alert_id, notification.probability, notification.threshold) == (
        alert.id,
        100,
        100,
    )


async def test_lower_bounds_do_not_fire(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """threshold=1 con probability=0 no dispara; el mínimo del umbral no es 0 (D4)."""
    field = await make_field()
    await make_alert(field, event_type=EventType.FROST, threshold=1)
    await make_forecast(field, forecast_date=TODAY, event_type=EventType.FROST, probability=0)

    assert await _evaluate(db_session) == 0
    assert await _notifications(db_session) == []


async def test_no_alerts_returns_zero(
    db_session: AsyncSession,
    make_field: MakeField,
    make_forecast: MakeForecast,
) -> None:
    field = await make_field()
    for offset in range(3):
        await make_forecast(
            field,
            forecast_date=TODAY + timedelta(days=offset),
            event_type=EventType.FROST,
            probability=100,
        )

    assert await _evaluate(db_session) == 0
    assert await _notifications(db_session) == []


async def test_no_forecasts_returns_zero(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    field = await make_field()
    await make_alert(field, event_type=EventType.FROST, threshold=1)

    assert await _evaluate(db_session) == 0
    assert await _notifications(db_session) == []


# --- "Hoy" en los bordes de mes / año -------------------------------------


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 12, 31, 12, 0, tzinfo=UTC),
        datetime(2027, 1, 1, 12, 0, tzinfo=UTC),
    ],
)
async def test_today_at_month_and_year_boundary(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
    now: datetime,
) -> None:
    """El corte `forecast_date >= hoy local` es correcto cruzando fin de mes y de año."""
    # 12:00Z es el mismo día calendario en Buenos Aires (09:00 -03:00).
    today = now.date()
    field = await make_field()
    await make_alert(field, event_type=EventType.FROST, threshold=50)
    for offset in (-1, 0, 1):
        await make_forecast(
            field,
            forecast_date=today + timedelta(days=offset),
            event_type=EventType.FROST,
            probability=80,
            issued_at=now,
        )

    created = await _evaluate(db_session, now=now)

    assert created == 2
    dates = {notification.forecast_date for notification in await _notifications(db_session)}
    assert dates == {today, today + timedelta(days=1)}


# --- Cambios entre corridas ------------------------------------------------


async def test_forecast_crossing_threshold_between_runs_notifies_later(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """Un día que arranca bajo el umbral y luego lo supera se notifica en la corrida siguiente."""
    field = await make_field()
    await make_alert(field, event_type=EventType.FROST, threshold=70)
    forecast = await make_forecast(
        field, forecast_date=TODAY + timedelta(days=2), event_type=EventType.FROST, probability=40
    )

    assert await _evaluate(db_session) == 0

    forecast.probability = 85  # la ingesta actualiza el pronóstico (upsert)
    await db_session.flush()

    assert await _evaluate(db_session) == 1
    [notification] = await _notifications(db_session)
    assert notification.probability == 85
    assert notification.forecast_date == TODAY + timedelta(days=2)


async def test_lowering_threshold_between_runs_notifies_only_new_days(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """Bajar el umbral (PATCH) notifica los días que ahora califican y no duplica
    los ya notificados."""
    field = await make_field()
    alert = await make_alert(field, event_type=EventType.FROST, threshold=90)
    for offset, probability in ((0, 50), (1, 90), (2, 95)):
        await make_forecast(
            field,
            forecast_date=TODAY + timedelta(days=offset),
            event_type=EventType.FROST,
            probability=probability,
        )

    assert await _evaluate(db_session) == 2

    alert.threshold = 50
    await db_session.flush()

    # Solo el día 0, que ahora cumple 50 >= 50; los días 1 y 2 siguen con su snapshot.
    assert await _evaluate(db_session) == 1
    notifications = await _notifications(db_session, alert_id=alert.id)
    assert [n.forecast_date for n in notifications] == [
        TODAY,
        TODAY + timedelta(days=1),
        TODAY + timedelta(days=2),
    ]
    assert {n.forecast_date: n.threshold for n in notifications}[TODAY + timedelta(days=1)] == 90
    assert {n.forecast_date: n.threshold for n in notifications}[TODAY] == 50


async def test_reactivation_notifies_only_days_not_yet_notified(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """Pausar y reactivar (PATCH is_active) solo genera los días que faltaban."""
    field = await make_field()
    alert = await make_alert(field, event_type=EventType.FROST, threshold=60)
    await make_forecast(field, forecast_date=TODAY, event_type=EventType.FROST, probability=80)
    await make_forecast(
        field, forecast_date=TODAY + timedelta(days=1), event_type=EventType.FROST, probability=80
    )

    assert await _evaluate(db_session) == 2

    alert.is_active = False
    await db_session.flush()
    # Un día nuevo llega mientras la alerta está pausada.
    await make_forecast(
        field, forecast_date=TODAY + timedelta(days=2), event_type=EventType.FROST, probability=80
    )
    assert await _evaluate(db_session) == 0
    assert len(await _notifications(db_session, alert_id=alert.id)) == 2

    alert.is_active = True
    await db_session.flush()

    assert await _evaluate(db_session) == 1
    assert await _evaluate(db_session) == 0
    notifications = await _notifications(db_session, alert_id=alert.id)
    assert [n.forecast_date for n in notifications] == [
        TODAY,
        TODAY + timedelta(days=1),
        TODAY + timedelta(days=2),
    ]


async def test_several_thresholds_for_the_same_field_and_event_only_fire_the_crossed_ones(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """D5/S9: 30 (atención) / 60 / 90 (crítico) con probabilidad 70 → dos avisos, no tres.

    Cada alerta conserva su propio `threshold` en el snapshot, así el usuario recibe un aviso
    por cada nivel superado y ninguno por el nivel que todavía no se alcanzó.
    """
    field = await make_field()
    thresholds = [30, 60, 90]
    alerts = {
        threshold: await make_alert(field, event_type=EventType.FROST, threshold=threshold)
        for threshold in thresholds
    }
    await make_forecast(field, forecast_date=TODAY, event_type=EventType.FROST, probability=70)

    created = await _evaluate(db_session)

    assert created == 2
    notifications = await _notifications(db_session)
    assert {notification.alert_id for notification in notifications} == {
        alerts[30].id,
        alerts[60].id,
    }
    assert {(n.alert_id, n.threshold) for n in notifications} == {
        (alerts[30].id, 30),
        (alerts[60].id, 60),
    }
    # Re-ejecutar no agrega el nivel 90 ni duplica los otros dos.
    assert await _evaluate(db_session) == 0


async def test_reactivating_by_direct_write_does_not_reset_the_day_state(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """El evaluador solo sigue `alert_day_states`: el re-armado por PATCH lo hace el service.

    Escribir `is_active` directo (sin pasar por `update_alert`) no re-arma el día, así que
    un día ya avisado y desarmado sigue sin re-avisarse aunque la alerta vuelva a estar
    activa. El camino real (PATCH) sí re-arma y se prueba en `tests/api/test_alerts.py`.
    """
    field = await make_field()
    alert = await make_alert(field, event_type=EventType.FROST, threshold=60)
    await make_forecast(field, forecast_date=TODAY, event_type=EventType.FROST, probability=80)
    assert await _evaluate(db_session) == 1
    [notification] = await _notifications(db_session, alert_id=alert.id)

    # Pausa + lo que hace el dispatcher en el claim (D12): pending -> cancelled.
    alert.is_active = False
    notification.status = NotificationStatus.CANCELLED
    notification.last_error = "alert_inactive"
    await db_session.flush()
    assert await _evaluate(db_session) == 0

    alert.is_active = True
    await db_session.flush()
    assert await _evaluate(db_session) == 0

    [still_cancelled] = await _notifications(db_session, alert_id=alert.id)
    assert still_cancelled.id == notification.id
    assert still_cancelled.status == NotificationStatus.CANCELLED
    assert still_cancelled.last_error == "alert_inactive"
    assert await _day_state(db_session, alert.id, TODAY) == DayState(episode=1, armed=False)


async def test_soft_deleted_alert_keeps_its_history_and_a_new_alert_notifies_again(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """Borrar lógicamente conserva el historial; una alerta nueva sobre el mismo
    (campo, evento, umbral) vuelve a notificar esos días (S9/D11)."""
    field = await make_field()
    alert = await make_alert(field, event_type=EventType.FROST, threshold=60)
    for offset in range(2):
        await make_forecast(
            field,
            forecast_date=TODAY + timedelta(days=offset),
            event_type=EventType.FROST,
            probability=80,
        )

    assert await _evaluate(db_session) == 2
    assert len(await _notifications(db_session)) == 2

    alert.deleted_at = FIXED_AT
    await db_session.flush()
    # El borrado lógico no toca las notificaciones y el evaluador ya no evalúa la alerta.
    assert len(await _notifications(db_session)) == 2
    assert await _evaluate(db_session) == 0
    # Tampoco toca sus días: la alerta no está en los candidatos, así que el estado sigue
    # desarmado (no re-arma ni dispara).
    for offset in range(2):
        assert await _day_state(db_session, alert.id, TODAY + timedelta(days=offset)) == DayState(
            episode=1, armed=False
        )

    new_alert = await make_alert(field, event_type=EventType.FROST, threshold=60)
    assert await _evaluate(db_session) == 2
    notifications = await _notifications(db_session)
    assert {n.alert_id for n in notifications} == {alert.id, new_alert.id}
    assert len(notifications) == 4
    # La alerta nueva tiene su propio estado, independiente del de la borrada.
    assert await _day_state(db_session, new_alert.id, TODAY) == DayState(episode=1, armed=False)


# --- Volumen ---------------------------------------------------------------


async def test_thirty_days_five_events_multiple_alerts_counted_exactly(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """30 días x 5 eventos x 2 campos (una alerta por campo/evento): 300, sin duplicados."""
    fields = [await make_field(), await make_field()]
    alerts: list[Alert] = []
    for field in fields:
        for event_type in EventType:
            alerts.append(await make_alert(field, event_type=event_type, threshold=50))

    expected: set[tuple[int, date]] = set()
    for field in fields:
        for offset in range(30):
            day = TODAY + timedelta(days=offset)
            for event_type in EventType:
                await make_forecast(field, forecast_date=day, event_type=event_type, probability=60)
    for alert in alerts:
        for offset in range(30):
            expected.add((alert.id, TODAY + timedelta(days=offset)))

    created = await _evaluate(db_session)

    assert created == 300
    notifications = await _notifications(db_session)
    assert len(notifications) == 300
    assert {(n.alert_id, n.forecast_date) for n in notifications} == expected
    # Re-ejecutar no agrega nada.
    assert await _evaluate(db_session) == 0


# --- Concurrencia ----------------------------------------------------------


async def _evaluate_and_commit(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
    lock_wait_ms: int = DEFAULT_LOCK_WAIT_MS,
) -> int:
    """Corre el evaluador en su propia conexión y commitea (libera locks de la transacción)."""
    async with session_factory() as session:
        created = await evaluate_alerts(
            session,
            now=now,
            horizon_days=0,
            max_forecast_age_hours=MAX_AGE_HOURS,
            future_tolerance_minutes=FUTURE_TOLERANCE_MINUTES,
            rearm_margin=REARM_MARGIN,
            lock_wait_ms=lock_wait_ms,
        )
        await session.commit()
        return created


async def _lock_timeout_of(session: AsyncSession) -> str:
    """`lock_timeout` vigente de la conexión (`SHOW lock_timeout`)."""
    return (await session.execute(text("SHOW lock_timeout"))).scalar_one()


async def test_a_busy_evaluation_lock_skips_the_cycle(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """S14a: si el lock no se libera antes de la espera, no se evalúa y se loguea el skip.

    El skip es un warning con la espera usada; el `lock_timeout` de la conexión queda como
    estaba y la sesión sigue usable. Al liberarse el lock, el ciclo siguiente evalúa
    normalmente y dispara el episodio.
    """
    field = await make_field()
    alert = await make_alert(field, event_type=EventType.FROST, threshold=70)
    await make_forecast(field, forecast_date=TODAY, event_type=EventType.FROST, probability=80)
    previous_lock_timeout = await _lock_timeout_of(db_session)

    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as holder:
            # Como un PATCH de alerta con el lock compartido abierto (o otra réplica evaluando).
            await holder.execute(
                text("SELECT pg_advisory_xact_lock_shared(:key)"),
                {"key": EVALUATOR_LOCK_KEY},
            )
            caplog.set_level(logging.INFO, logger="app.jobs.evaluator")

            assert await _evaluate(db_session, lock_wait_ms=LOCK_WAIT_MS) == 0
            assert await _notifications(db_session) == []
            assert await _day_states(db_session) == []
            # El savepoint revirtió el `SET LOCAL`: la conexión conserva su `lock_timeout`.
            assert await _lock_timeout_of(db_session) == previous_lock_timeout
            [record] = [
                record for record in caplog.records if "evaluation_skipped" in record.getMessage()
            ]
            assert record.levelno == logging.WARNING
            assert record.getMessage() == (
                f"evaluation_skipped reason=lock_busy wait_ms={LOCK_WAIT_MS}"
            )
            await holder.rollback()

        # La sesión sigue usable: sin el lock, el ciclo siguiente evalúa y dispara el episodio.
        caplog.clear()
        assert await _evaluate(db_session) == 1
        assert await _day_state(db_session, alert.id, TODAY) == DayState(episode=1, armed=False)
    finally:
        await engine.dispose()


async def test_the_evaluation_runs_when_the_lock_is_released_within_the_wait(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """S14a: el pedido exclusivo espera en la cola y evalúa si el lock se libera a tiempo."""
    field = await make_field()
    alert = await make_alert(field, event_type=EventType.FROST, threshold=70)
    await make_forecast(field, forecast_date=TODAY, event_type=EventType.FROST, probability=80)

    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as holder:
            await holder.execute(
                text("SELECT pg_advisory_xact_lock_shared(:key)"),
                {"key": EVALUATOR_LOCK_KEY},
            )
            evaluation = asyncio.create_task(_evaluate(db_session, lock_wait_ms=LOCK_WAIT_MS * 10))
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(evaluation), timeout=BLOCKED_TIMEOUT)
            assert not evaluation.done()
            # Al liberarse (commit/rollback del otro), la evaluación encolada continúa.
            await holder.rollback()
        assert await asyncio.wait_for(evaluation, timeout=5) == 1
    finally:
        await engine.dispose()

    assert await _day_state(db_session, alert.id, TODAY) == DayState(episode=1, armed=False)


async def _advisory_lock_waiter_exists(engine: AsyncEngine, key: int) -> bool:
    """¿Hay un pedido por `key` esperando en la cola del lock (no otorgado todavía)?"""
    async with engine.connect() as conn:
        waiting = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM pg_locks "
                    "WHERE locktype = 'advisory' AND NOT granted "
                    "AND ((classid::bigint << 32) | objid::bigint) = :key"
                ),
                {"key": key},
            )
        ).scalar_one()
    return bool(waiting)


async def _wait_until(condition: Callable[[], Awaitable[bool]], *, limit: float = 5.0) -> None:
    """Espera (poll corto) a que se cumpla una condición de la base; falla si no llega."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit
    while loop.time() < deadline:
        if await condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("la condición esperada no se cumplió a tiempo")


def _sqlstate(exc: DBAPIError) -> str | None:
    """sqlstate del error de Postgres (asyncpg lo expone en `orig` o su `__cause__`)."""
    orig = exc.orig
    for candidate in (orig, getattr(orig, "__cause__", None)):
        sqlstate = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
        if sqlstate is not None:
            return str(sqlstate)
    return None


async def test_a_new_shared_lock_waits_behind_a_queued_evaluator(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """S14a SEC-1: con el evaluador encolado, un `lock_shared` nuevo no lo adelanta.

    Es lo que impide que un flujo constante de PATCH deje al evaluador sin turno: el pedido
    exclusivo encolado hace esperar a los PATCH nuevos; en la API esa espera termina en 503
    (`DB_LOCK_TIMEOUT_MS`) y el PATCH se reintenta.
    """
    field = await make_field()
    await make_alert(field, event_type=EventType.FROST, threshold=70)
    await make_forecast(field, forecast_date=TODAY, event_type=EventType.FROST, probability=80)

    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as open_patch:
            # Un PATCH abierto (lock compartido) tiene el lock: el evaluador queda encolado.
            await open_patch.execute(
                text("SELECT pg_advisory_xact_lock_shared(:key)"),
                {"key": EVALUATOR_LOCK_KEY},
            )
            evaluation = asyncio.create_task(_evaluate(db_session, lock_wait_ms=LOCK_WAIT_MS * 20))
            try:
                await _wait_until(lambda: _advisory_lock_waiter_exists(engine, EVALUATOR_LOCK_KEY))
                # Un PATCH nuevo pide el lock compartido con un `lock_timeout` corto: está
                # detrás del evaluador encolado y no lo consigue.
                async with engine.connect() as newcomer:
                    await newcomer.execute(
                        select(func.set_config("lock_timeout", str(LOCK_WAIT_MS), True))
                    )
                    with pytest.raises(DBAPIError) as caught:
                        await newcomer.execute(
                            text("SELECT pg_advisory_xact_lock_shared(:key)"),
                            {"key": EVALUATOR_LOCK_KEY},
                        )
                assert _sqlstate(caught.value) == "55P03"

                # Al liberarse el PATCH abierto, el evaluador (que estaba primero) evalúa.
                await open_patch.rollback()
                assert await asyncio.wait_for(evaluation, timeout=5) == 1
            finally:
                if not evaluation.done():
                    evaluation.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await evaluation
    finally:
        await engine.dispose()


async def test_a_busy_lock_does_not_rearm_a_fired_day() -> None:
    """S14a: el skip tampoco re-arma; el estado queda intacto hasta que el lock se libera."""
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    user_id: int | None = None
    try:
        user_id, field_id, (alert_id,) = await _seed_lock_scenario(session_factory)
        assert await _evaluate_and_commit(session_factory, now=NOW) == 1

        # El pronóstico baja del `rearm_level`: el próximo ciclo debería re-armar el día.
        async with engine.begin() as conn:
            await conn.execute(
                update(WeatherForecast)
                .where(WeatherForecast.field_id == field_id)
                .values(probability=0)
            )

        async with engine.connect() as holder:
            await holder.execute(
                text("SELECT pg_advisory_xact_lock_shared(:key)"),
                {"key": EVALUATOR_LOCK_KEY},
            )
            assert (
                await _evaluate_and_commit(session_factory, now=NOW, lock_wait_ms=LOCK_WAIT_MS) == 0
            )
            await holder.rollback()

        assert await _stored_day_state(session_factory, alert_id) == (1, False)

        # Sin el lock, el ciclo re-arma (y no crea notificaciones).
        assert await _evaluate_and_commit(session_factory, now=NOW) == 0
        assert await _stored_day_state(session_factory, alert_id) == (1, True)
    finally:
        await _cleanup_owner(engine, user_id)


async def test_the_busy_lock_log_is_a_warning_that_names_the_wait(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """El skip es una señal de que hay contención: se loguea en WARNING con `wait_ms` (S14a)."""
    caplog.set_level(logging.INFO, logger="app.jobs.evaluator")
    field = await make_field()
    await make_alert(field, event_type=EventType.FROST, threshold=70)
    await make_forecast(field, forecast_date=TODAY, event_type=EventType.FROST, probability=90)

    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as holder:
            await holder.execute(
                text("SELECT pg_advisory_xact_lock_shared(:key)"),
                {"key": EVALUATOR_LOCK_KEY},
            )
            assert await _evaluate(db_session, lock_wait_ms=LOCK_WAIT_MS) == 0
            await holder.rollback()
    finally:
        await engine.dispose()

    [record] = [record for record in caplog.records if "evaluation_skipped" in record.getMessage()]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == f"evaluation_skipped reason=lock_busy wait_ms={LOCK_WAIT_MS}"


#: `lock_timeout` de la transacción que distinguen los dos valores posibles (la espera del
#: lock y el default de la conexión en la base de test, que es `0`).
TRANSACTION_LOCK_TIMEOUT = "2500ms"


async def test_a_skipped_lock_restores_the_transaction_lock_timeout(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """S14a: tras el skip, ni la espera ni el default de la conexión: el valor de la transacción.

    El `lock_timeout` de la espera vive solo dentro del savepoint: al vencer y hacer rollback,
    la transacción queda con el valor que tenía (aquí uno distinto de `0` y de la espera) y
    sigue usable — el ciclo siguiente, en la misma transacción, evalúa y dispara.
    """
    field = await make_field()
    alert = await make_alert(field, event_type=EventType.FROST, threshold=70)
    await make_forecast(field, forecast_date=TODAY, event_type=EventType.FROST, probability=80)
    await db_session.execute(text(f"SET LOCAL lock_timeout = '{TRANSACTION_LOCK_TIMEOUT}'"))
    assert await _lock_timeout_of(db_session) == TRANSACTION_LOCK_TIMEOUT

    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as holder:
            await holder.execute(
                text("SELECT pg_advisory_xact_lock_shared(:key)"),
                {"key": EVALUATOR_LOCK_KEY},
            )
            assert await _evaluate(db_session, lock_wait_ms=LOCK_WAIT_MS) == 0
            await holder.rollback()
    finally:
        await engine.dispose()

    # Ni `200ms` (la espera) ni `0` (la conexión): el valor que tenía la transacción.
    assert await _lock_timeout_of(db_session) == TRANSACTION_LOCK_TIMEOUT
    assert await _notifications(db_session) == []
    assert await _day_states(db_session) == []
    # La sesión sigue usable en la misma transacción: el ciclo siguiente evalúa el día.
    assert await _evaluate(db_session) == 1
    assert await _day_state(db_session, alert.id, TODAY) == DayState(episode=1, armed=False)
    assert await _lock_timeout_of(db_session) == TRANSACTION_LOCK_TIMEOUT


async def test_a_successful_evaluation_restores_the_transaction_lock_timeout(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """S14a: tomar el lock no deja `lock_timeout` en la espera; se restaura al de la transacción."""
    field = await make_field()
    await make_alert(field, event_type=EventType.FROST, threshold=70)
    await make_forecast(field, forecast_date=TODAY, event_type=EventType.FROST, probability=80)
    await db_session.execute(text(f"SET LOCAL lock_timeout = '{TRANSACTION_LOCK_TIMEOUT}'"))

    assert await _evaluate(db_session, lock_wait_ms=LOCK_WAIT_MS) == 1

    assert await _lock_timeout_of(db_session) == TRANSACTION_LOCK_TIMEOUT
    # El resto de la evaluación corrió sin dejar el GUC de la espera: una sentencia posterior
    # funciona y la transacción sigue abierta.
    assert (await db_session.execute(text("SELECT 1"))).scalar_one() == 1
    assert await _lock_timeout_of(db_session) == TRANSACTION_LOCK_TIMEOUT


async def test_a_non_lock_timeout_error_while_taking_the_lock_is_reraised(
    db_session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """S14a: solo `55P03` es un skip; cualquier otro error de DB al pedir el lock se re-lanza.

    Se fuerza un error real de Postgres distinto de `55P03` (`22023`, valor de `lock_timeout`
    fuera de rango) al fijar la espera, sin mocks de la base.
    """
    caplog.set_level(logging.INFO, logger="app.jobs.evaluator")
    previous = await _lock_timeout_of(db_session)

    with pytest.raises(DBAPIError) as caught:
        await _take_evaluation_lock(db_session, lock_wait_ms=-1)

    assert _sqlstate(caught.value) != "55P03"
    assert "lock_timeout" in str(caught.value)
    assert not [record for record in caplog.records if "evaluation_skipped" in record.getMessage()]
    # El savepoint se revirtió: el GUC que tenía la transacción y la sesión siguen usables.
    assert await _lock_timeout_of(db_session) == previous
    assert (await db_session.execute(text("SELECT 1"))).scalar_one() == 1


async def test_two_concurrent_evaluations_create_exactly_the_total() -> None:
    """Dos evaluadores en conexiones distintas y datos ya commiteados: la suma es el total
    exacto y no hay duplicados (RNF-03)."""
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    user_id: int | None = None
    try:
        async with session_factory() as setup:
            user = User(
                phone_number=f"+54911{uuid.uuid4().int % 10**8:08d}",
                name=f"QA concurrent {suffix}",
            )
            setup.add(user)
            await setup.flush()
            field = Field(
                user_id=user.id,
                name=f"QA concurrent field {suffix}",
                latitude=Decimal("-34.6"),
                longitude=Decimal("-58.4"),
            )
            setup.add(field)
            await setup.flush()
            alerts = [
                Alert(field_id=field.id, event_type=event_type, threshold=50, is_active=True)
                for event_type in EventType
            ]
            setup.add_all(alerts)
            await setup.flush()
            days = [TODAY + timedelta(days=offset) for offset in range(3)]
            setup.add_all(
                WeatherForecast(
                    field_id=field.id,
                    forecast_date=day,
                    event_type=event_type,
                    probability=75,
                    source="mock",
                    issued_at=FIXED_AT,
                    fetched_at=FIXED_AT,
                )
                for day in days
                for event_type in EventType
            )
            alert_ids = [alert.id for alert in alerts]
            await setup.commit()
            user_id = user.id
        expected = len(days) * len(EventType)

        first, second = await asyncio.gather(
            _evaluate_and_commit(session_factory, now=NOW),
            _evaluate_and_commit(session_factory, now=NOW),
        )

        assert first + second == expected
        async with session_factory() as check:
            rows = (
                await check.execute(
                    select(Notification.alert_id, Notification.forecast_date).where(
                        Notification.alert_id.in_(alert_ids)
                    )
                )
            ).all()
        pairs = {(row.alert_id, row.forecast_date) for row in rows}
        assert len(rows) == expected
        assert len(pairs) == expected
    finally:
        if user_id is not None:
            async with engine.begin() as conn:
                await conn.execute(delete(User).where(User.id == user_id))
        await engine.dispose()


async def test_two_concurrent_evaluations_do_not_duplicate_episodes() -> None:
    """Dos conexiones separadas sobre los mismos datos: un episodio, una notificación.

    Con el lock pedido en cola (S14a) la segunda evaluación espera a que la primera commitee,
    ve el día ya disparado (`armed = false`) y devuelve 0: la suma es 1. Sin el lock, cada
    réplica podría re-armar y disparar el mismo día con su propio snapshot (episodio
    duplicado); la red de seguridad `UNIQUE (alert_id, forecast_date, episode)` quedaría como
    segunda barrera.
    """
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    user_id: int | None = None
    try:
        async with session_factory() as setup:
            user = User(
                phone_number=f"+54911{uuid.uuid4().int % 10**8:08d}",
                name=f"Episodes concurrent {suffix}",
            )
            setup.add(user)
            await setup.flush()
            field = Field(
                user_id=user.id,
                name=f"Episodes concurrent field {suffix}",
                latitude=Decimal("-34.6"),
                longitude=Decimal("-58.4"),
            )
            setup.add(field)
            await setup.flush()
            alert = Alert(
                field_id=field.id, event_type=EventType.FROST, threshold=50, is_active=True
            )
            setup.add(alert)
            await setup.flush()
            setup.add(
                WeatherForecast(
                    field_id=field.id,
                    forecast_date=TODAY,
                    event_type=EventType.FROST,
                    probability=90,
                    source="mock",
                    issued_at=FIXED_AT,
                    fetched_at=FIXED_AT,
                )
            )
            alert_id = alert.id
            await setup.commit()
            user_id = user.id

        first, second = await asyncio.gather(
            _evaluate_and_commit(session_factory, now=NOW),
            _evaluate_and_commit(session_factory, now=NOW),
        )

        assert first + second == 1
        async with session_factory() as check:
            notifications = (
                await check.execute(
                    select(Notification.forecast_date, Notification.episode).where(
                        Notification.alert_id == alert_id
                    )
                )
            ).all()
            states = (
                await check.execute(
                    select(
                        AlertDayState.forecast_date,
                        AlertDayState.episode,
                        AlertDayState.armed,
                    ).where(AlertDayState.alert_id == alert_id)
                )
            ).all()
        assert [tuple(row) for row in notifications] == [(TODAY, 1)]
        assert [tuple(row) for row in states] == [(TODAY, 1, False)]
    finally:
        if user_id is not None:
            async with engine.begin() as conn:
                await conn.execute(delete(User).where(User.id == user_id))
        await engine.dispose()


async def test_the_evaluation_takes_the_advisory_lock(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """Con la transacción del evaluador abierta, otra conexión no puede tomar el lock."""
    field = await make_field()
    await make_alert(field, event_type=EventType.FROST, threshold=70)
    await make_forecast(field, forecast_date=TODAY, event_type=EventType.FROST, probability=90)

    assert await _evaluate(db_session) == 1

    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            free = (
                await conn.execute(
                    text("SELECT pg_try_advisory_xact_lock(:key)"),
                    {"key": EVALUATOR_LOCK_KEY},
                )
            ).scalar_one()
        assert free is False
    finally:
        await engine.dispose()


# --- Lock del PATCH contra el evaluador (S12b SEC-1/CR-1) -------------------

#: Tiempo máximo para probar que una tarea **está** bloqueada. No es una fuente de verdad:
#: el bloqueo se prueba con que la tarea recién termina al liberarse el lock.
BLOCKED_TIMEOUT = 0.2

#: Espera del lock en los tests de S14a: corta para que el skip sea rápido.
LOCK_WAIT_MS = 200


async def _cleanup_owner(engine: AsyncEngine, user_id: int | None) -> None:
    """Borra el usuario del escenario (cascada) y libera el engine."""
    if user_id is not None:
        async with engine.begin() as conn:
            await conn.execute(delete(User).where(User.id == user_id))
    await engine.dispose()


async def _seed_lock_scenario(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    alerts: list[tuple[EventType, int, int]] | None = None,
) -> tuple[int, int, list[int]]:
    """Commitea un usuario con un campo y sus alertas/pronósticos.

    `alerts` = `(event_type, threshold, probability)`. Devuelve `(user_id, field_id,
    alert_ids)` ya commiteados: las conexiones del test y del PATCH los ven.
    """
    suffix = uuid.uuid4().hex
    plans = alerts if alerts is not None else [(EventType.FROST, 70, 80)]
    async with session_factory() as setup:
        user = User(
            phone_number=f"+54911{uuid.uuid4().int % 10**8:08d}",
            name=f"Lock scenario {suffix}",
        )
        setup.add(user)
        await setup.flush()
        field = Field(
            user_id=user.id,
            name=f"Lock field {suffix}",
            latitude=Decimal("-34.6"),
            longitude=Decimal("-58.4"),
        )
        setup.add(field)
        await setup.flush()
        alert_ids: list[int] = []
        for event_type, threshold, probability in plans:
            alert = Alert(
                field_id=field.id,
                event_type=event_type,
                threshold=threshold,
                is_active=True,
            )
            setup.add(alert)
            await setup.flush()
            alert_ids.append(alert.id)
            setup.add(
                WeatherForecast(
                    field_id=field.id,
                    forecast_date=TODAY,
                    event_type=event_type,
                    probability=probability,
                    source="mock",
                    issued_at=FIXED_AT,
                    fetched_at=FIXED_AT,
                )
            )
        user_id, field_id = user.id, field.id
        await setup.commit()
    return user_id, field_id, alert_ids


async def _patch_threshold(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: int,
    alert_id: int,
    threshold: int,
) -> Alert:
    """PATCH de umbral con su propia conexión y transacción (como el router)."""
    async with session_factory() as session:
        return await update_alert(session, user_id, alert_id, AlertUpdate(threshold=threshold))


async def test_patch_waits_for_the_evaluator_lock() -> None:
    """Con el lock exclusivo del evaluador abierto, el PATCH espera y termina al liberarse."""
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    user_id: int | None = None
    try:
        user_id, _, (alert_id,) = await _seed_lock_scenario(session_factory)
        async with engine.connect() as holder:
            # Simula una evaluación en curso: lock exclusivo sin commitear.
            await holder.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": EVALUATOR_LOCK_KEY}
            )
            patch = asyncio.create_task(
                _patch_threshold(session_factory, user_id=user_id, alert_id=alert_id, threshold=90)
            )
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(patch), timeout=BLOCKED_TIMEOUT)
            assert not patch.done()
            # Al liberarse (commit/rollback del worker) el PATCH continúa y aplica el cambio.
            await holder.rollback()
        updated = await asyncio.wait_for(patch, timeout=5)
        assert updated.threshold == 90
    finally:
        await _cleanup_owner(engine, user_id)


async def test_patch_of_a_missing_or_foreign_alert_returns_404_without_the_lock(
    client: AsyncClient,
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    """S14a SEC-1: el 404 se resuelve antes de pedir el lock compartido.

    Con una evaluación en curso (lock exclusivo abierto en otra conexión) un PATCH a una
    alerta inexistente o ajena vuelve enseguida: si pidiera el lock primero, esperaría.
    """
    owner = await make_user()
    field = await make_field(owner)
    alert = await make_alert(field, event_type=EventType.FROST, threshold=70)
    other = await make_user()

    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as holder:
            # Simula una evaluación en curso: lock exclusivo sin commitear.
            await holder.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": EVALUATOR_LOCK_KEY}
            )
            missing = await asyncio.wait_for(
                client.patch(
                    f"{ALERTS_API}/999999999",
                    json={"threshold": 90},
                    headers=auth(other),
                ),
                timeout=5,
            )
            foreign = await asyncio.wait_for(
                client.patch(
                    f"{ALERTS_API}/{alert.id}",
                    json={"threshold": 90},
                    headers=auth(other),
                ),
                timeout=5,
            )
            await holder.rollback()
    finally:
        await engine.dispose()

    assert missing.status_code == 404
    assert missing.json() == {"detail": "Alert not found"}
    assert foreign.status_code == 404
    assert foreign.json() == {"detail": "Alert not found"}


async def test_patch_of_an_own_alert_waits_for_the_evaluation_lock(
    client: AsyncClient,
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    """S14a: el PATCH de una alerta propia **sí** espera al lock exclusivo y aplica el cambio.

    El lock compartido se toma después de la ownership y antes del UPDATE (JOBS.md
    §Re-armado por cambio de alerta): con una evaluación en curso, el PATCH queda esperando.
    """
    owner = await make_user()
    field = await make_field(owner)
    alert = await make_alert(field, event_type=EventType.FROST, threshold=70)

    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as holder:
            await holder.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": EVALUATOR_LOCK_KEY}
            )
            patch = asyncio.create_task(
                client.patch(
                    f"{ALERTS_API}/{alert.id}", json={"threshold": 90}, headers=auth(owner)
                )
            )
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(patch), timeout=BLOCKED_TIMEOUT)
            assert not patch.done()
            await holder.rollback()
        response = await asyncio.wait_for(patch, timeout=5)
    finally:
        await engine.dispose()

    assert response.status_code == 200
    assert response.json()["threshold"] == 90


async def test_a_patch_waiting_behind_the_evaluator_lock_returns_503(
    client: AsyncClient,
    db_session: AsyncSession,
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    """S14a: si la espera del PATCH detrás del evaluador supera `DB_LOCK_TIMEOUT_MS` → 503.

    JOBS.md §Re-armado por cambio de alerta: el PATCH es reintentable. El `lock_timeout`
    corto de la transacción emula el `DB_LOCK_TIMEOUT_MS` del engine de la API.
    """
    owner = await make_user()
    field = await make_field(owner)
    alert = await make_alert(field, event_type=EventType.FROST, threshold=70)
    await db_session.execute(text(f"SET LOCAL lock_timeout = '{LOCK_WAIT_MS}ms'"))

    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as holder:
            await holder.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": EVALUATOR_LOCK_KEY}
            )
            response = await asyncio.wait_for(
                client.patch(
                    f"{ALERTS_API}/{alert.id}", json={"threshold": 90}, headers=auth(owner)
                ),
                timeout=5,
            )
            await holder.rollback()
    finally:
        await engine.dispose()

    assert response.status_code == 503
    assert response.json() == {"detail": "Database busy, try again later"}
    assert response.headers["Retry-After"] == "1"


async def test_two_concurrent_patches_of_different_alerts_do_not_block() -> None:
    """El lock es compartido: un PATCH en vuelo no bloquea el PATCH de otra alerta."""
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    user_id: int | None = None
    try:
        user_id, _, (first_id, second_id) = await _seed_lock_scenario(
            session_factory,
            alerts=[(EventType.FROST, 70, 80), (EventType.RAIN, 70, 80)],
        )
        async with engine.connect() as holder:
            # Como un PATCH a la primera alerta que todavía no commiteó.
            await holder.execute(
                text("SELECT pg_advisory_xact_lock_shared(:key)"),
                {"key": EVALUATOR_LOCK_KEY},
            )
            updated = await asyncio.wait_for(
                _patch_threshold(
                    session_factory, user_id=user_id, alert_id=second_id, threshold=90
                ),
                timeout=5,
            )
            assert updated.id == second_id
            assert updated.threshold == 90
            # La transacción del primer PATCH sigue abierta: no hubo bloqueo mutuo.
            assert holder.in_transaction()
            await holder.rollback()
        async with session_factory() as check:
            untouched = (
                await check.execute(select(Alert.threshold).where(Alert.id == first_id))
            ).scalar_one()
        assert untouched == 70
    finally:
        await _cleanup_owner(engine, user_id)


async def test_threshold_patch_racing_an_evaluation_leaves_a_coherent_state() -> None:
    """SEC-1: evaluación y PATCH de umbral concurrentes dejan un estado coherente.

    Orden A (evaluación en vuelo): el evaluador dispara con el umbral viejo (70) y no
    commitea; el PATCH del umbral 90 espera el lock exclusivo y, al liberarse, re-arma y
    cancela la notificación. Nunca queda una `pending` con el umbral viejo.

    Orden B (PATCH primero): el evaluador ve el umbral nuevo (90) y el 80% no dispara.
    """
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    user_ids: list[int] = []
    try:
        # --- Orden A: el evaluador dispara y el PATCH llega mientras no commiteó.
        user_a, _, (alert_a,) = await _seed_lock_scenario(session_factory)
        user_ids.append(user_a)
        async with session_factory() as evaluator:
            # Lock exclusivo + episodio 1 insertado, todo sin commitear.
            assert await _evaluate(evaluator) == 1
            patch = asyncio.create_task(
                _patch_threshold(session_factory, user_id=user_a, alert_id=alert_a, threshold=90)
            )
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(patch), timeout=BLOCKED_TIMEOUT)
            assert not patch.done()
            await evaluator.commit()
        updated_a = await asyncio.wait_for(patch, timeout=5)
        assert updated_a.threshold == 90

        async with session_factory() as check:
            notifications = (
                await check.execute(
                    select(
                        Notification.status,
                        Notification.threshold,
                        Notification.episode,
                        Notification.last_error,
                    ).where(Notification.alert_id == alert_a)
                )
            ).all()
            states = (
                await check.execute(
                    select(AlertDayState.episode, AlertDayState.armed).where(
                        AlertDayState.alert_id == alert_a
                    )
                )
            ).all()
            stale_pending = (
                await check.execute(
                    select(func.count())
                    .select_from(Notification)
                    .where(
                        Notification.alert_id == alert_a,
                        Notification.status == NotificationStatus.PENDING,
                        Notification.threshold != 90,
                    )
                )
            ).scalar_one()
        # El aviso salió con el umbral viejo, pero el re-armado lo canceló (sin duplicados).
        assert [tuple(row) for row in notifications] == [
            (NotificationStatus.CANCELLED, 70, 1, "alert_reset")
        ]
        # El día quedó re-armado: el próximo ciclo re-avisa con el umbral vigente.
        assert [tuple(row) for row in states] == [(1, True)]
        assert stale_pending == 0

        # --- Orden B: el PATCH commitea antes de la evaluación.
        user_b, _, (alert_b,) = await _seed_lock_scenario(session_factory)
        user_ids.append(user_b)
        updated_b = await _patch_threshold(
            session_factory, user_id=user_b, alert_id=alert_b, threshold=90
        )
        assert updated_b.threshold == 90
        assert await _evaluate_and_commit(session_factory, now=NOW) == 0
        async with session_factory() as check:
            notifications_b = (
                await check.execute(
                    select(func.count())
                    .select_from(Notification)
                    .where(Notification.alert_id == alert_b)
                )
            ).scalar_one()
            states_b = (
                await check.execute(
                    select(func.count())
                    .select_from(AlertDayState)
                    .where(AlertDayState.alert_id == alert_b)
                )
            ).scalar_one()
        assert notifications_b == 0
        assert states_b == 0
    finally:
        for owner in user_ids:
            if owner is not None:
                async with engine.begin() as conn:
                    await conn.execute(delete(User).where(User.id == owner))
        await engine.dispose()


# --- Equivalencia SQL vs. la función pura de episodios ---------------------

#: Semilla fija: la secuencia es determinística y se puede reproducir.
EQUIVALENCE_SEED = 20261117
EQUIVALENCE_CYCLES = 40
#: Umbrales que puede elegir el PATCH simulado (incluye el mismo: no re-arma).
THRESHOLD_CHOICES = (50, 60, 70, 80, 90)


async def test_sql_evaluator_matches_the_pure_step_and_reset_functions(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """Secuencia determinística (semilla fija) con PATCH de umbral y pausa/reactivación.

    Cada ciclo se corre dos veces: el evaluador SQL (con `app/services/alerts.update_alert`
    para los PATCH, que es quien re-arma) y la función pura `step`/`reset`. Deben coincidir
    en notificaciones (día, episodio) y en el estado final del día.
    """
    rng = random.Random(EQUIVALENCE_SEED)
    field = await make_field()
    alert = await make_alert(field, event_type=EventType.FROST, threshold=70)
    forecast = await make_forecast(
        field, forecast_date=TODAY, event_type=EventType.FROST, probability=50
    )

    threshold = 70
    active = True
    probability = 50
    state = INITIAL_STATE
    expected: list[tuple[date, int]] = []
    threshold_changes = 0
    reactivations = 0

    for cycle in range(EQUIVALENCE_CYCLES):
        roll = rng.random()
        if roll < 0.15:
            new_threshold = rng.choice(THRESHOLD_CHOICES)
            if new_threshold != threshold:
                threshold = new_threshold
                threshold_changes += 1
                state = reset(state)
                await update_alert(
                    db_session, field.user_id, alert.id, AlertUpdate(threshold=threshold)
                )
        elif roll < 0.25:
            active = not active
            if active:  # reactivación: re-armado
                reactivations += 1
                state = reset(state)
            await update_alert(db_session, field.user_id, alert.id, AlertUpdate(is_active=active))

        probability = min(
            100,
            max(
                0,
                probability
                + rng.choice((-40, -25, -15, -10, -5, 0, 5, 10, 15, 25, 40))
                + rng.randint(-5, 5),
            ),
        )
        forecast.probability = probability
        await db_session.flush()

        notifies = False
        if active:
            state, notifies = step(
                state,
                probability=probability,
                threshold=threshold,
                rearm_margin=REARM_MARGIN,
            )
        if notifies:
            expected.append((TODAY, state.episode))

        created = await _evaluate(db_session)
        assert created == int(notifies), (
            f"cycle={cycle} probability={probability} threshold={threshold} "
            f"active={active} expected_notify={notifies} created={created}"
        )

    notifications = await _notifications(db_session, alert_id=alert.id)
    assert [(n.forecast_date, n.episode) for n in notifications] == expected
    assert await _day_state(db_session, alert.id, TODAY) == state
    # La secuencia debe haber ejercitado lo que este test dice cubrir.
    assert expected, "la secuencia no generó ninguna notificación"
    assert threshold_changes and reactivations


# --- Frescura de los pronósticos (S11, D14) -------------------------------


STALE_AT = NOW - timedelta(hours=MAX_AGE_HOURS)


async def test_a_fresh_forecast_fires(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    field = await make_field()
    await make_alert(field, event_type=EventType.FROST, threshold=70)
    await make_forecast(
        field,
        forecast_date=TODAY,
        event_type=EventType.FROST,
        probability=90,
        issued_at=NOW - timedelta(hours=1),
    )

    assert await _evaluate(db_session) == 1


async def test_a_stale_forecast_does_not_fire(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """Emitido antes de `now - 24 h` (por un segundo): no dispara (regla 7)."""
    field = await make_field()
    await make_alert(field, event_type=EventType.FROST, threshold=70)
    await make_forecast(
        field,
        forecast_date=TODAY,
        event_type=EventType.FROST,
        probability=100,
        issued_at=STALE_AT - timedelta(seconds=1),
    )

    assert await _evaluate(db_session) == 0
    assert await _notifications(db_session) == []


async def test_the_exact_freshness_boundary_still_fires(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """`issued_at == now - max_forecast_age_hours` dispara: el corte es inclusivo."""
    field = await make_field()
    await make_alert(field, event_type=EventType.FROST, threshold=70)
    await make_forecast(
        field,
        forecast_date=TODAY,
        event_type=EventType.FROST,
        probability=100,
        issued_at=STALE_AT,
    )

    assert await _evaluate(db_session) == 1


async def test_the_freshness_window_is_configurable(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """Con una ventana más chica, el mismo pronóstico deja de evaluarse."""
    field = await make_field()
    await make_alert(field, event_type=EventType.FROST, threshold=70)
    await make_forecast(
        field,
        forecast_date=TODAY,
        event_type=EventType.FROST,
        probability=100,
        issued_at=NOW - timedelta(hours=2),
    )

    assert await _evaluate(db_session, max_forecast_age_hours=1) == 0
    # El corte es inclusivo también con una ventana no default.
    assert await _evaluate(db_session, max_forecast_age_hours=2) == 1


async def test_stale_forecasts_are_reported_with_counts(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """El warning cuenta los pronósticos viejos y los campos afectados (sin traer filas)."""
    caplog.set_level(logging.WARNING, logger="app.jobs.evaluator")

    stale_a = await make_field()
    await make_alert(stale_a, event_type=EventType.FROST, threshold=70)
    await make_forecast(
        stale_a,
        forecast_date=TODAY,
        event_type=EventType.FROST,
        probability=90,
        issued_at=STALE_AT - timedelta(hours=1),
    )

    stale_b = await make_field()
    await make_alert(stale_b, event_type=EventType.RAIN, threshold=50)
    for offset in range(2):
        await make_forecast(
            stale_b,
            forecast_date=TODAY + timedelta(days=offset),
            event_type=EventType.RAIN,
            probability=80,
            issued_at=STALE_AT - timedelta(days=2),
        )

    # Alertas borradas o inactivas, días pasados u otro evento no cuentan.
    deleted = await make_field()
    deleted_alert = await make_alert(deleted, event_type=EventType.HAIL, threshold=50)
    deleted_alert.deleted_at = FIXED_AT
    await make_forecast(
        deleted,
        forecast_date=TODAY,
        event_type=EventType.HAIL,
        probability=100,
        issued_at=STALE_AT - timedelta(days=3),
    )
    paused = await make_field()
    await make_alert(paused, event_type=EventType.HEAT_WAVE, threshold=50, is_active=False)
    await make_forecast(
        paused,
        forecast_date=TODAY,
        event_type=EventType.HEAT_WAVE,
        probability=100,
        issued_at=STALE_AT - timedelta(days=3),
    )
    past = await make_field()
    await make_alert(past, event_type=EventType.STRONG_WIND, threshold=50)
    await make_forecast(
        past,
        forecast_date=TODAY - timedelta(days=1),
        event_type=EventType.STRONG_WIND,
        probability=100,
        issued_at=STALE_AT - timedelta(days=3),
    )
    other_event = await make_field()
    await make_alert(other_event, event_type=EventType.FROST, threshold=50)
    await make_forecast(
        other_event,
        forecast_date=TODAY,
        event_type=EventType.RAIN,
        probability=100,
        issued_at=STALE_AT - timedelta(days=3),
    )
    await db_session.flush()

    assert await _evaluate(db_session) == 0
    assert await _notifications(db_session) == []
    messages = [record.getMessage() for record in caplog.records]
    assert "stale_forecasts count=3 fields=2 max_age_hours=24" in messages


async def test_no_stale_warning_when_every_forecast_is_fresh(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="app.jobs.evaluator")
    field = await make_field()
    await make_alert(field, event_type=EventType.FROST, threshold=70)
    await make_forecast(
        field,
        forecast_date=TODAY,
        event_type=EventType.FROST,
        probability=90,
        issued_at=NOW,
    )

    assert await _evaluate(db_session) == 1
    assert "stale_forecasts" not in caplog.text


async def test_a_stale_forecast_then_a_fresh_issue_the_same_day_fires_once(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """El pronóstico viejo no dispara; cuando la ingesta trae uno fresco del mismo día, sí.

    La ingesta pisa la misma fila `(campo, día, evento)` con un `issued_at` nuevo: el
    evaluador lo acepta en la corrida siguiente y el dedupe evita un segundo aviso.
    """
    field = await make_field()
    await make_alert(field, event_type=EventType.FROST, threshold=70)
    forecast = await make_forecast(
        field,
        forecast_date=TODAY,
        event_type=EventType.FROST,
        probability=95,
        issued_at=STALE_AT - timedelta(hours=1),
    )

    assert await _evaluate(db_session) == 0
    assert await _notifications(db_session) == []

    # Reingesta fresca del mismo día (upsert sobre la misma clave natural).
    forecast.issued_at = NOW
    forecast.source = "fresh"
    await db_session.flush()

    assert await _evaluate(db_session) == 1
    assert await _evaluate(db_session) == 0
    [notification] = await _notifications(db_session)
    assert (notification.forecast_date, notification.probability, notification.threshold) == (
        TODAY,
        95,
        70,
    )


async def test_issued_at_with_a_non_utc_offset_is_compared_in_absolute_time(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """`issued_at` con offset se compara en tiempo absoluto: el mismo instante es el borde.

    09:00-03:00 == 12:00Z == `now` (fresco); 14:00+02:00 del día anterior == `now - 24 h`
    (borde inclusivo, dispara); 13:59:59+02:00 -- un segundo antes del corte -- es viejo.
    """
    fresh = await make_field()
    fresh_alert = await make_alert(fresh, event_type=EventType.FROST, threshold=70)
    fresh_forecast = await make_forecast(
        fresh,
        forecast_date=TODAY,
        event_type=EventType.FROST,
        probability=90,
        issued_at=datetime(2026, 11, 16, 9, 0, tzinfo=timezone(timedelta(hours=-3))),
    )

    boundary = await make_field()
    boundary_alert = await make_alert(boundary, event_type=EventType.FROST, threshold=70)
    await make_forecast(
        boundary,
        forecast_date=TODAY,
        event_type=EventType.FROST,
        probability=90,
        issued_at=datetime(2026, 11, 15, 14, 0, tzinfo=timezone(timedelta(hours=2))),
    )

    stale = await make_field()
    await make_alert(stale, event_type=EventType.FROST, threshold=70)
    await make_forecast(
        stale,
        forecast_date=TODAY,
        event_type=EventType.FROST,
        probability=90,
        issued_at=datetime(2026, 11, 15, 13, 59, 59, tzinfo=timezone(timedelta(hours=2))),
    )

    created = await _evaluate(db_session)

    assert created == 2
    await db_session.refresh(fresh_forecast)
    # asyncpg devuelve `timestamptz` normalizado a UTC: mismo instante, otra representación.
    assert fresh_forecast.issued_at == NOW
    assert {n.alert_id for n in await _notifications(db_session)} == {
        fresh_alert.id,
        boundary_alert.id,
    }


async def test_no_stale_warning_for_fields_without_alerts(
    db_session: AsyncSession,
    make_field: MakeField,
    make_forecast: MakeForecast,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Un campo sin alertas con pronósticos viejos no cuenta en el warning (no se evalúa)."""
    caplog.set_level(logging.WARNING, logger="app.jobs.evaluator")
    field = await make_field()
    for offset in range(3):
        await make_forecast(
            field,
            forecast_date=TODAY + timedelta(days=offset),
            event_type=EventType.FROST,
            probability=100,
            issued_at=STALE_AT - timedelta(days=5),
        )

    assert await _evaluate(db_session) == 0
    assert await _notifications(db_session) == []
    assert "stale_forecasts" not in caplog.text


async def test_stale_warning_counts_each_forecast_once_with_several_thresholds(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """S9 permite varios umbrales por (campo, evento): un solo pronóstico viejo sigue siendo uno.

    El warning se define en docs/JOBS.md §Evaluador como `count=<n>` pronósticos viejos y
    `fields=<m>` campos distintos, así que el fan-out del JOIN con las alertas no debe inflar
    el conteo.
    """
    caplog.set_level(logging.WARNING, logger="app.jobs.evaluator")
    field = await make_field()
    await make_alert(field, event_type=EventType.FROST, threshold=50)
    await make_alert(field, event_type=EventType.FROST, threshold=80)
    await make_forecast(
        field,
        forecast_date=TODAY,
        event_type=EventType.FROST,
        probability=100,
        issued_at=STALE_AT - timedelta(hours=1),
    )

    assert await _evaluate(db_session) == 0
    messages = [record.getMessage() for record in caplog.records]
    assert "stale_forecasts count=1 fields=1 max_age_hours=24" in messages


async def test_no_stale_warning_when_all_stale_forecasts_belong_to_paused_or_deleted_alerts(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Solo cuentan las alertas activas y no borradas, aunque no haya ninguna fresca."""
    caplog.set_level(logging.WARNING, logger="app.jobs.evaluator")

    paused = await make_field()
    await make_alert(paused, event_type=EventType.FROST, threshold=70, is_active=False)
    await make_forecast(
        paused,
        forecast_date=TODAY,
        event_type=EventType.FROST,
        probability=100,
        issued_at=STALE_AT - timedelta(days=1),
    )

    deleted = await make_field()
    deleted_alert = await make_alert(deleted, event_type=EventType.RAIN, threshold=50)
    deleted_alert.deleted_at = FIXED_AT
    await make_forecast(
        deleted,
        forecast_date=TODAY,
        event_type=EventType.RAIN,
        probability=100,
        issued_at=STALE_AT - timedelta(days=1),
    )
    await db_session.flush()

    assert await _evaluate(db_session) == 0
    assert "stale_forecasts" not in caplog.text


async def test_a_stale_forecast_below_every_threshold_is_not_counted(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """QA-1/CR-3: el warning cuenta pronósticos que *habrían disparado*; uno bajo el umbral no."""
    caplog.set_level(logging.WARNING, logger="app.jobs.evaluator")
    field = await make_field()
    await make_alert(field, event_type=EventType.FROST, threshold=70)
    await make_forecast(
        field,
        forecast_date=TODAY,
        event_type=EventType.FROST,
        probability=69,
        issued_at=STALE_AT - timedelta(hours=1),
    )

    assert await _evaluate(db_session) == 0
    assert await _notifications(db_session) == []
    assert "stale_forecasts" not in caplog.text


async def test_a_forecast_issued_too_far_in_the_future_is_not_evaluated(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """S11 SEC-1: defensa del evaluador ante un `issued_at > now + tolerancia`.

    La ingesta ya acota ese dato; si por algún camino quedó guardado, el evaluador tampoco lo
    usa (ni lo cuenta como viejo: queda fuera de ambas ventanas).
    """
    field = await make_field()
    await make_alert(field, event_type=EventType.FROST, threshold=70)
    await make_forecast(
        field,
        forecast_date=TODAY,
        event_type=EventType.FROST,
        probability=100,
        issued_at=NOW + timedelta(minutes=FUTURE_TOLERANCE_MINUTES + 1),
    )

    assert await _evaluate(db_session) == 0
    assert await _notifications(db_session) == []


async def test_a_forecast_within_the_future_tolerance_is_evaluated(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """El borde `issued_at == now + tolerancia` se acepta (tolerancia de reloj inclusiva)."""
    field = await make_field()
    await make_alert(field, event_type=EventType.FROST, threshold=70)
    await make_forecast(
        field,
        forecast_date=TODAY,
        event_type=EventType.FROST,
        probability=100,
        issued_at=NOW + timedelta(minutes=FUTURE_TOLERANCE_MINUTES),
    )

    assert await _evaluate(db_session) == 1


# --- "Hoy" local por campo y horizonte (S12a, D9/D2) -----------------------


async def test_local_today_uses_the_field_timezone(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """A las 01:00Z del 17 en Buenos Aires todavía es 16; en UTC ya es 17 (D9)."""
    now = datetime(2026, 11, 17, 1, 0, tzinfo=UTC)

    buenos_aires = await make_field(name="Lote Buenos Aires")
    buenos_aires.timezone = "America/Argentina/Buenos_Aires"
    utc_field = await make_field(name="Lote UTC")
    utc_field.timezone = "UTC"
    await db_session.flush()

    ba_alert = await make_alert(buenos_aires, event_type=EventType.FROST, threshold=50)
    await make_alert(utc_field, event_type=EventType.FROST, threshold=50)
    await make_forecast(
        buenos_aires,
        forecast_date=date(2026, 11, 16),
        event_type=EventType.FROST,
        probability=80,
        issued_at=now,
    )
    await make_forecast(
        utc_field,
        forecast_date=date(2026, 11, 16),
        event_type=EventType.FROST,
        probability=80,
        issued_at=now,
    )

    created = await _evaluate(db_session, now=now)

    assert created == 1
    [notification] = await _notifications(db_session)
    assert notification.alert_id == ba_alert.id
    assert notification.forecast_date == date(2026, 11, 16)


async def test_a_timezone_ahead_of_utc_uses_its_own_local_today(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """A las 20:00Z del 16 en Tokio ya es el 17: el día local 16 quedó atrás."""
    now = datetime(2026, 11, 16, 20, 0, tzinfo=UTC)

    tokyo = await make_field(name="Lote Tokio")
    tokyo.timezone = "Asia/Tokyo"
    await db_session.flush()
    alert = await make_alert(tokyo, event_type=EventType.RAIN, threshold=50)
    for day in (date(2026, 11, 16), date(2026, 11, 17)):
        await make_forecast(
            tokyo,
            forecast_date=day,
            event_type=EventType.RAIN,
            probability=80,
            issued_at=now,
        )

    created = await _evaluate(db_session, now=now)

    assert created == 1
    [notification] = await _notifications(db_session)
    assert notification.alert_id == alert.id
    assert notification.forecast_date == date(2026, 11, 17)


async def test_the_horizon_is_relative_to_the_fields_local_today(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """Ventana `[hoy_local, hoy_local + 7)`: hoy+6 dispara, hoy+7 no (D2)."""
    now = datetime(2026, 11, 17, 1, 0, tzinfo=UTC)  # hoy local 16 en Buenos Aires

    field = await make_field(name="Lote Horizonte")
    field.timezone = "America/Argentina/Buenos_Aires"
    await db_session.flush()
    await make_alert(field, event_type=EventType.HAIL, threshold=50)
    today = date(2026, 11, 16)
    inside = today + timedelta(days=6)
    outside = today + timedelta(days=7)
    for day in (inside, outside):
        await make_forecast(
            field,
            forecast_date=day,
            event_type=EventType.HAIL,
            probability=80,
            issued_at=now,
        )

    assert await _evaluate(db_session, now=now, horizon_days=7) == 1
    [notification] = await _notifications(db_session)
    assert notification.forecast_date == inside

    # `0` = sin límite superior: el día que quedaba afuera ahora dispara.
    assert await _evaluate(db_session, now=now, horizon_days=0) == 1
    dates = {n.forecast_date for n in await _notifications(db_session)}
    assert dates == {inside, outside}


async def test_a_horizon_of_zero_has_no_upper_bound(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    field = await make_field()
    await make_alert(field, event_type=EventType.HEAT_WAVE, threshold=40)
    far = TODAY + timedelta(days=365)
    await make_forecast(
        field,
        forecast_date=far,
        event_type=EventType.HEAT_WAVE,
        probability=60,
        issued_at=NOW,
    )

    assert await _evaluate(db_session, now=NOW, horizon_days=0) == 1
    [notification] = await _notifications(db_session)
    assert notification.forecast_date == far


def test_the_local_today_and_horizon_are_computed_in_sql() -> None:
    """El "hoy" sale de `AT TIME ZONE fields.timezone`; no se calcula en Python (S12a)."""
    sql = str(
        _build_statement(
            now=NOW,
            horizon_days=7,
            max_forecast_age_hours=MAX_AGE_HOURS,
            future_tolerance_minutes=FUTURE_TOLERANCE_MINUTES,
        ).compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )

    assert "AT TIME ZONE fields.timezone" in sql
    assert "CAST(" in sql
    assert "fields.timezone" in sql
    assert "weather_forecasts.forecast_date <" in sql


# --- Pronósticos con `issued_at` adelantado (S12a SEC-4) -------------------


async def test_future_forecasts_are_reported_with_counts(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """El warning cuenta pronósticos excluidos por adelantados y sus campos (SEC-4)."""
    caplog.set_level(logging.WARNING, logger="app.jobs.evaluator")
    too_far = NOW + timedelta(minutes=FUTURE_TOLERANCE_MINUTES + 1)

    first = await make_field()
    await make_alert(first, event_type=EventType.FROST, threshold=70)
    await make_forecast(
        first,
        forecast_date=TODAY,
        event_type=EventType.FROST,
        probability=90,
        issued_at=too_far,
    )
    second = await make_field()
    await make_alert(second, event_type=EventType.RAIN, threshold=50)
    for offset in range(2):
        await make_forecast(
            second,
            forecast_date=TODAY + timedelta(days=offset),
            event_type=EventType.RAIN,
            probability=80,
            issued_at=too_far,
        )
    # Una alerta pausada no cuenta (no se evalúa).
    paused = await make_field()
    await make_alert(paused, event_type=EventType.HAIL, threshold=50, is_active=False)
    await make_forecast(
        paused,
        forecast_date=TODAY,
        event_type=EventType.HAIL,
        probability=100,
        issued_at=too_far,
    )
    await db_session.flush()

    assert await _evaluate(db_session, now=NOW) == 0
    messages = [record.getMessage() for record in caplog.records]
    assert "future_forecasts count=3 fields=2 tolerance_minutes=60" in messages


async def test_no_future_warning_when_every_forecast_is_within_the_tolerance(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="app.jobs.evaluator")
    field = await make_field()
    await make_alert(field, event_type=EventType.FROST, threshold=70)
    await make_forecast(
        field,
        forecast_date=TODAY,
        event_type=EventType.FROST,
        probability=90,
        issued_at=NOW + timedelta(minutes=FUTURE_TOLERANCE_MINUTES),
    )

    assert await _evaluate(db_session, now=NOW) == 1
    assert "future_forecasts" not in caplog.text


async def test_future_forecast_outside_the_horizon_is_not_counted(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """El warning sólo cuenta la ventana evaluada: un día fuera del horizonte no cuenta."""
    caplog.set_level(logging.WARNING, logger="app.jobs.evaluator")
    field = await make_field()
    await make_alert(field, event_type=EventType.FROST, threshold=70)
    await make_forecast(
        field,
        forecast_date=TODAY + timedelta(days=7),
        event_type=EventType.FROST,
        probability=90,
        issued_at=NOW + timedelta(minutes=FUTURE_TOLERANCE_MINUTES + 1),
    )

    assert await _evaluate(db_session, now=NOW, horizon_days=7) == 0
    assert "future_forecasts" not in caplog.text
