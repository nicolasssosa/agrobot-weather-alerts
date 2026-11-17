"""Bordes del "hoy" local por campo y del horizonte de evaluación (S12a, D9/D2).

Ver docs/JOBS.md §Evaluador (regla 3), docs/REQUIREMENTS.md D2/D9 y
docs/DATA_MODEL.md §fields. Todos los casos usan un `now` fijo: nunca el reloj real.
"""

import logging
from collections.abc import Awaitable
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs.evaluator import (
    _build_out_of_window_statement,
    _build_statement,
    evaluate_alerts,
)
from app.models import Alert, EventType, Field, Notification
from tests.conftest import MakeAlert, MakeField, MakeForecast, MakeUser

MAX_AGE_HOURS = 24
FUTURE_TOLERANCE_MINUTES = 60
REARM_MARGIN = 10


def _at(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """Instante UTC fijo."""
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _evaluate(
    session: AsyncSession,
    *,
    now: datetime,
    horizon_days: int = 0,
    max_forecast_age_hours: int = MAX_AGE_HOURS,
) -> Awaitable[int]:
    """Corre el evaluador con `now` y la ventana de frescura fijos."""
    return evaluate_alerts(
        session,
        now=now,
        horizon_days=horizon_days,
        max_forecast_age_hours=max_forecast_age_hours,
        future_tolerance_minutes=FUTURE_TOLERANCE_MINUTES,
        rearm_margin=REARM_MARGIN,
    )


async def _field_in(
    db_session: AsyncSession, make_field: MakeField, timezone: str, *, name: str | None = None
) -> Field:
    """Campo con una zona IANA concreta (el factory no expone `timezone`)."""
    field = await make_field(name=name)
    field.timezone = timezone
    await db_session.flush()
    return field


async def _notifications(session: AsyncSession) -> list[Notification]:
    """Notificaciones ordenadas por alerta y día."""
    result = await session.execute(
        select(Notification).order_by(Notification.alert_id, Notification.forecast_date)
    )
    return list(result.scalars().all())


# --- Cambio de horario de verano (DST) -------------------------------------


@pytest.mark.parametrize(
    ("now", "today"),
    [
        # DST termina en Madrid el 2026-10-25 a las 01:00Z: a las 22:30Z el offset ya es +01
        # (23:30 local). Un offset fijo de verano (+02) daría 00:30 del 26 y evaluaría otro día.
        (_at(2026, 10, 25, 22, 30), date(2026, 10, 25)),
        # DST empieza el 2026-03-29 a las 01:00Z: a las 22:30Z del 28 el offset es +01 (23:30
        # local). Un offset de verano daría el 29.
        (_at(2026, 3, 28, 22, 30), date(2026, 3, 28)),
    ],
)
async def test_the_dst_changeover_does_not_shift_the_local_day(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
    now: datetime,
    today: date,
) -> None:
    """`AT TIME ZONE` aplica la regla DST vigente: el "hoy" local no se corre un día."""
    field = await _field_in(db_session, make_field, "Europe/Madrid", name="Lote Madrid")
    await make_alert(field, event_type=EventType.FROST, threshold=50)
    for day in (today, today + timedelta(days=1)):
        await make_forecast(
            field,
            forecast_date=day,
            event_type=EventType.FROST,
            probability=80,
            issued_at=now,
        )

    created = await _evaluate(db_session, now=now, horizon_days=1)

    assert created == 1
    [notification] = await _notifications(db_session)
    assert notification.forecast_date == today


# --- Offsets que no son horas enteras --------------------------------------


@pytest.mark.parametrize(
    ("timezone", "now", "today"),
    [
        # +05:30 → 23:50 del 15.
        ("Asia/Kolkata", _at(2026, 11, 15, 18, 20), date(2026, 11, 15)),
        # +05:45 → 00:05 del 16: el cuarto de hora cambia el día.
        ("Asia/Kathmandu", _at(2026, 11, 15, 18, 20), date(2026, 11, 16)),
        # +13:45 (DST del hemisferio sur en noviembre) → 23:59 del 15.
        ("Pacific/Chatham", _at(2026, 11, 15, 10, 14), date(2026, 11, 15)),
        # +12:45 (horario estándar de Chatham, junio) → 23:59 del 15.
        ("Pacific/Chatham", _at(2026, 6, 15, 11, 14), date(2026, 6, 15)),
        # `Etc/GMT+3` es UTC-3 (el signo de la base `Etc` está invertido) → 23:30 del 15.
        ("Etc/GMT+3", _at(2026, 11, 16, 2, 30), date(2026, 11, 15)),
        ("UTC", _at(2026, 11, 16, 2, 30), date(2026, 11, 16)),
    ],
)
async def test_local_today_handles_offsets_that_are_not_whole_hours(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
    timezone: str,
    now: datetime,
    today: date,
) -> None:
    """Con horizonte 1 solo dispara el día local de hoy, también con offsets :30/:45."""
    field = await _field_in(db_session, make_field, timezone, name=f"Lote {timezone}")
    await make_alert(field, event_type=EventType.RAIN, threshold=50)
    for day in (today - timedelta(days=1), today, today + timedelta(days=1)):
        await make_forecast(
            field,
            forecast_date=day,
            event_type=EventType.RAIN,
            probability=80,
            issued_at=now,
        )

    created = await _evaluate(db_session, now=now, horizon_days=1)

    assert created == 1
    [notification] = await _notifications(db_session)
    assert notification.forecast_date == today


@pytest.mark.parametrize(
    ("timezone", "now", "today"),
    [
        # Medianoche local exacta en Chatham (+13:45): 10:15Z son las 00:00 del 16.
        ("Pacific/Chatham", _at(2026, 11, 15, 10, 15), date(2026, 11, 16)),
        ("UTC", _at(2026, 11, 16, 0, 0), date(2026, 11, 16)),
    ],
)
async def test_the_local_midnight_belongs_to_the_new_day(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
    timezone: str,
    now: datetime,
    today: date,
) -> None:
    """El borde es `forecast_date >= hoy_local`: la medianoche exacta ya es "hoy"."""
    field = await _field_in(db_session, make_field, timezone, name=f"Lote {timezone}")
    await make_alert(field, event_type=EventType.HAIL, threshold=50)
    for day in (today - timedelta(days=1), today):
        await make_forecast(
            field,
            forecast_date=day,
            event_type=EventType.HAIL,
            probability=80,
            issued_at=now,
        )

    created = await _evaluate(db_session, now=now)

    assert created == 1
    [notification] = await _notifications(db_session)
    assert notification.forecast_date == today


# --- Dos campos con el mismo pronóstico y la misma hora UTC ----------------


async def _extreme_fields(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
    *,
    now: datetime,
) -> tuple[tuple[Field, Alert], tuple[Field, Alert]]:
    """Campos en +14 y -11 con pronósticos idénticos para el 16 y el 17."""
    east = await _field_in(db_session, make_field, "Pacific/Kiritimati", name="Lote +14")
    west = await _field_in(db_session, make_field, "Pacific/Pago_Pago", name="Lote -11")
    created: list[tuple[Field, Alert]] = []
    for field in (east, west):
        alert = await make_alert(field, event_type=EventType.FROST, threshold=50)
        created.append((field, alert))
        for day in (date(2026, 11, 16), date(2026, 11, 17)):
            await make_forecast(
                field,
                forecast_date=day,
                event_type=EventType.FROST,
                probability=80,
                issued_at=now,
            )
    return created[0], created[1]


async def test_two_fields_at_the_same_utc_hour_start_a_different_local_day(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """12:00Z del 16: en Kiritimati (+14) ya es el 17; en Pago Pago (-11) es el 16.

    Mismos pronósticos guardados y misma corrida: con horizonte 1 dispara un día distinto en
    cada campo, así que la misma fila de `weather_forecasts` notifica en uno y no en el otro.
    """
    now = _at(2026, 11, 16, 12, 0)
    (_, east_alert), (_, west_alert) = await _extreme_fields(
        db_session, make_field, make_alert, make_forecast, now=now
    )

    created = await _evaluate(db_session, now=now, horizon_days=1)

    assert created == 2
    pairs = {
        (notification.alert_id, notification.forecast_date)
        for notification in await _notifications(db_session)
    }
    assert pairs == {
        (east_alert.id, date(2026, 11, 17)),
        (west_alert.id, date(2026, 11, 16)),
    }


async def test_without_a_horizon_each_field_still_starts_its_window_today(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """Sin horizonte, el piso del "hoy" local sigue discriminando por campo (+14 vs -11)."""
    now = _at(2026, 11, 16, 12, 0)
    (_, east_alert), (_, west_alert) = await _extreme_fields(
        db_session, make_field, make_alert, make_forecast, now=now
    )

    created = await _evaluate(db_session, now=now, horizon_days=0)

    assert created == 3
    pairs = {
        (notification.alert_id, notification.forecast_date)
        for notification in await _notifications(db_session)
    }
    # Kiritimati ya está en el 17: el 16 local quedó atrás. Pago Pago evalúa ambos días.
    assert pairs == {
        (east_alert.id, date(2026, 11, 17)),
        (west_alert.id, date(2026, 11, 16)),
        (west_alert.id, date(2026, 11, 17)),
    }


# --- Horizonte: bordes 1 y 365 --------------------------------------------


async def test_a_horizon_of_one_evaluates_only_the_local_today(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """`horizon_days=1` deja la ventana `[hoy, hoy+1)`: solo hoy."""
    now = _at(2026, 11, 16, 12, 0)
    field = await _field_in(db_session, make_field, "America/Argentina/Buenos_Aires")
    await make_alert(field, event_type=EventType.HAIL, threshold=50)
    today = date(2026, 11, 16)
    for day in (today - timedelta(days=1), today, today + timedelta(days=1)):
        await make_forecast(
            field,
            forecast_date=day,
            event_type=EventType.HAIL,
            probability=80,
            issued_at=now,
        )

    created = await _evaluate(db_session, now=now, horizon_days=1)

    assert created == 1
    [notification] = await _notifications(db_session)
    assert notification.forecast_date == today


async def test_the_horizon_boundary_of_365_days_is_exclusive(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """`horizon_days=365` incluye hoy+364 y excluye hoy+365 (borde `<`)."""
    now = _at(2026, 11, 16, 12, 0)
    field = await _field_in(db_session, make_field, "America/Argentina/Buenos_Aires")
    await make_alert(field, event_type=EventType.HEAT_WAVE, threshold=50)
    today = date(2026, 11, 16)
    inside = today + timedelta(days=364)
    outside = today + timedelta(days=365)
    for day in (inside, outside):
        await make_forecast(
            field,
            forecast_date=day,
            event_type=EventType.HEAT_WAVE,
            probability=80,
            issued_at=now,
        )

    created = await _evaluate(db_session, now=now, horizon_days=365)

    assert created == 1
    [notification] = await _notifications(db_session)
    assert notification.forecast_date == inside


# --- El horizonte también recorta el warning de pronósticos desactualizados --


async def test_stale_forecasts_are_counted_only_inside_the_horizon(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SEC-3/S11: el warning cuenta la ventana evaluada, no todo el pronóstico guardado."""
    caplog.set_level(logging.WARNING, logger="app.jobs.evaluator")
    now = _at(2026, 11, 16, 12, 0)
    field = await _field_in(db_session, make_field, "America/Argentina/Buenos_Aires")
    await make_alert(field, event_type=EventType.FROST, threshold=70)
    today = date(2026, 11, 16)
    stale_at = now - timedelta(hours=MAX_AGE_HOURS + 1)
    for day in (today + timedelta(days=6), today + timedelta(days=7)):
        await make_forecast(
            field,
            forecast_date=day,
            event_type=EventType.FROST,
            probability=90,
            issued_at=stale_at,
        )

    assert await _evaluate(db_session, now=now, horizon_days=7) == 0

    assert "stale_forecasts count=1 fields=1 max_age_hours=24" in caplog.text
    assert "count=2" not in caplog.text


async def test_no_stale_warning_when_the_stale_forecast_is_outside_the_horizon(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Un pronóstico viejo fuera del horizonte no se hubiera evaluado ni estando fresco."""
    caplog.set_level(logging.WARNING, logger="app.jobs.evaluator")
    now = _at(2026, 11, 16, 12, 0)
    field = await _field_in(db_session, make_field, "America/Argentina/Buenos_Aires")
    await make_alert(field, event_type=EventType.FROST, threshold=70)
    await make_forecast(
        field,
        forecast_date=date(2026, 11, 16) + timedelta(days=7),
        event_type=EventType.FROST,
        probability=90,
        issued_at=now - timedelta(hours=MAX_AGE_HOURS + 1),
    )

    assert await _evaluate(db_session, now=now, horizon_days=7) == 0

    assert "stale_forecasts" not in caplog.text


# --- Zonas que Postgres no conoce: nunca rompen el ciclo (S12a CR-1/SEC-1) ---


def _compiled_sql(*, horizon_days: int) -> str:
    """SQL que recibe Postgres para una corrida, con los binds literales."""
    return str(
        _build_statement(
            now=_at(2026, 11, 16, 12, 0),
            horizon_days=horizon_days,
            max_forecast_age_hours=MAX_AGE_HOURS,
            future_tolerance_minutes=FUTURE_TOLERANCE_MINUTES,
        ).compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


async def test_a_field_with_an_unknown_timezone_does_not_break_the_other_fields(
    db_session: AsyncSession,
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Una zona inválida insertada por SQL directo se saltea: el resto notifica igual.

    Sin la guarda `IN (SELECT name FROM pg_timezone_names)`, `AT TIME ZONE 'localtime'`
    abortaría el INSERT y ningún campo del sistema volvería a notificar (SEC-1).
    """
    caplog.set_level(logging.WARNING, logger="app.jobs.evaluator")
    now = _at(2026, 11, 16, 12, 0)
    today = date(2026, 11, 16)

    broken = await make_field(await make_user(), name="Lote con zona rota")
    await make_alert(broken, event_type=EventType.FROST, threshold=50)
    await make_forecast(
        broken,
        forecast_date=today,
        event_type=EventType.FROST,
        probability=90,
        issued_at=now,
    )
    healthy = await make_field(await make_user(), name="Lote sano")
    healthy_alert = await make_alert(healthy, event_type=EventType.FROST, threshold=50)
    await make_forecast(
        healthy,
        forecast_date=today,
        event_type=EventType.FROST,
        probability=90,
        issued_at=now,
    )
    await db_session.flush()

    # SQL directo: la API ya no acepta esta zona, pero una fila vieja puede tenerla.
    await db_session.execute(
        update(Field).where(Field.id == broken.id).values(timezone="localtime")
    )

    created = await _evaluate(db_session, now=now, horizon_days=1)

    assert created == 1
    [notification] = await _notifications(db_session)
    assert notification.alert_id == healthy_alert.id
    assert "invalid_timezone_fields count=1" in caplog.text


async def test_an_unknown_timezone_does_not_abort_a_hash_join_plan(
    db_session: AsyncSession,
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """CR-4/SEC-1: el `CASE` protege `AT TIME ZONE` también bajo un plan hash.

    El `IN` del `WHERE` no alcanza. Con volumen y estadísticas reales el planner arma un
    `Hash Join` de `fields` x `weather_forecasts` cuyo `Join Filter` calcula
    `AT TIME ZONE`, **antes** del semi join con `pg_timezone_names`: la fila de zona
    desconocida llega al filtro y Postgres aborta el ciclo entero con
    `time zone "localtime" not recognized`. El campo sano se inserta primero (peor caso:
    ya generó su fila) y el plan se fuerza a hash (`enable_nestloop`/`enable_mergejoin`
    off). Sin el `CASE` este test falla con ese error (verificado revirtiéndolo). Cubre
    el INSERT y, ejecutándola primero, la consulta de warnings.
    """
    caplog.set_level(logging.WARNING, logger="app.jobs.evaluator")
    now = _at(2026, 11, 16, 12, 0)
    today = date(2026, 11, 16)
    user = await make_user()

    healthy = await make_field(user, name="Lote sano CR-4")
    await make_alert(healthy, event_type=EventType.FROST, threshold=50)
    await make_forecast(
        healthy,
        forecast_date=today,
        event_type=EventType.FROST,
        probability=90,
        issued_at=now,
    )
    for index in range(100):
        field = await make_field(user, name=f"Lote válido CR-4 {index}")
        await make_alert(field, event_type=EventType.FROST, threshold=50)
        await make_forecast(
            field,
            forecast_date=today,
            event_type=EventType.FROST,
            probability=90,
            issued_at=now,
        )
    broken = await make_field(user, name="Lote con zona rota CR-4")
    await make_alert(broken, event_type=EventType.FROST, threshold=50)
    await make_forecast(
        broken,
        forecast_date=today,
        event_type=EventType.FROST,
        probability=90,
        issued_at=now,
    )
    await db_session.flush()

    # SQL directo: la API ya no acepta esta zona, pero una fila vieja puede tenerla.
    await db_session.execute(
        update(Field).where(Field.id == broken.id).values(timezone="localtime")
    )
    for table_analysis in (
        "ANALYZE fields",
        "ANALYZE alerts",
        "ANALYZE weather_forecasts",
    ):
        await db_session.execute(text(table_analysis))
    await db_session.execute(text("SET LOCAL enable_nestloop = off"))
    await db_session.execute(text("SET LOCAL enable_mergejoin = off"))

    # El semi join es un hash join (la forma que reproduce CR-4), no un nested loop que
    # descarta la fila rota antes de evaluar `AT TIME ZONE`.
    sql = _compiled_sql(horizon_days=1)
    plan = "\n".join((await db_session.execute(text("EXPLAIN " + sql))).scalars())
    assert any("Hash Cond" in line and "pg_timezone_names" in line for line in plan.splitlines()), (
        plan
    )

    # La consulta de warnings también arma el día local: se ejecuta antes del INSERT.
    assert (
        await db_session.execute(
            _build_out_of_window_statement(
                now=now,
                horizon_days=1,
                max_forecast_age_hours=MAX_AGE_HOURS,
                future_tolerance_minutes=FUTURE_TOLERANCE_MINUTES,
            )
        )
    ).one() == (0, 0, 0, 0)

    created = await _evaluate(db_session, now=now, horizon_days=1)

    assert created == 101
    notifications = await _notifications(db_session)
    assert len(notifications) == 101
    assert all(notification.forecast_date == today for notification in notifications)
    broken_alerts = set(
        (await db_session.execute(select(Alert.id).where(Alert.field_id == broken.id))).scalars()
    )
    assert broken_alerts
    assert not broken_alerts & {notification.alert_id for notification in notifications}
    assert "invalid_timezone_fields count=1" in caplog.text


async def test_no_invalid_timezone_warning_when_every_zone_is_known(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="app.jobs.evaluator")
    now = _at(2026, 11, 16, 12, 0)
    field = await _field_in(db_session, make_field, "America/Argentina/Buenos_Aires")
    await make_alert(field, event_type=EventType.RAIN, threshold=50)
    await make_forecast(
        field,
        forecast_date=date(2026, 11, 16),
        event_type=EventType.RAIN,
        probability=90,
        issued_at=now,
    )

    assert await _evaluate(db_session, now=now, horizon_days=1) == 1
    assert "invalid_timezone_fields" not in caplog.text


def test_the_sql_guards_the_timezone_and_has_a_constant_date_prefilter() -> None:
    """CR-1, CR-2 y CR-4: guarda de zona válida, prefiltro constante y `CASE` por fila."""
    sql = _compiled_sql(horizon_days=7)

    assert "pg_timezone_names" in sql
    assert "fields.timezone IN (SELECT" in sql
    # El `IN` del `WHERE` no garantiza el orden de evaluación: el día local va en un `CASE`.
    assert "CASE WHEN (fields.timezone IN (SELECT" in sql
    # Prefiltro constante con `now` en UTC: 2026-11-16 -> piso 15 y techo 24 (7 + 1).
    assert "weather_forecasts.forecast_date >= '2026-11-15'" in sql
    assert "weather_forecasts.forecast_date <= '2026-11-24'" in sql
    # El bound local exacto sigue presente.
    assert "AT TIME ZONE fields.timezone" in sql


def test_without_a_horizon_there_is_no_constant_upper_prefilter() -> None:
    sql = _compiled_sql(horizon_days=0)

    assert "weather_forecasts.forecast_date <= '" not in sql
    assert "pg_timezone_names" in sql


async def test_the_constant_date_prefilter_is_an_index_condition(
    db_session: AsyncSession,
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """CR-2: con el prefiltro, `forecast_date` entra como `Index Cond` del UNIQUE.

    El índice `UNIQUE (field_id, forecast_date, event_type)` sólo puede filtrar por rango si
    la condición es constante. `enable_seqscan = off` fuerza al planner a mostrarlo (las
    tablas del test son diminutas); `SET LOCAL` y `ANALYZE` se revierten con el test.
    """
    user = await make_user()
    for index in range(5):
        field = await make_field(user, name=f"Lote plan CR-2 {index}")
        await make_alert(field, event_type=EventType.FROST, threshold=50)
        await make_forecast(
            field,
            forecast_date=date(2026, 11, 16),
            event_type=EventType.FROST,
            probability=80,
            issued_at=_at(2026, 11, 16, 12, 0),
        )
    await db_session.flush()
    await db_session.execute(text("ANALYZE alerts"))
    await db_session.execute(text("ANALYZE weather_forecasts"))
    await db_session.execute(text("SET LOCAL enable_seqscan = off"))

    plan = "\n".join(
        (await db_session.execute(text("EXPLAIN " + _compiled_sql(horizon_days=7)))).scalars()
    )

    index_conditions = [line.strip() for line in plan.splitlines() if "Index Cond" in line]
    assert index_conditions, plan
    assert any("forecast_date >=" in condition for condition in index_conditions), plan
