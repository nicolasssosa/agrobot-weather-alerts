"""Tests del evaluador de alertas.

Ver docs/TESTING.md §`tests/jobs/test_evaluator.py` y docs/JOBS.md §Evaluador.
Todas las corridas usan un `today` fijo: nunca el reloj real.
"""

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.jobs.evaluator import _build_statement, evaluate_alerts
from app.models import (
    Alert,
    EventType,
    Field,
    Notification,
    NotificationStatus,
    User,
    WeatherForecast,
)
from tests.conftest import TEST_DATABASE_URL, MakeAlert, MakeField, MakeForecast, MakeUser

TODAY = date(2026, 11, 16)
#: Timestamp fijo para metadatos (`fetched_at`); nunca el reloj real.
FIXED_AT = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


async def _notifications(
    session: AsyncSession, *, alert_id: int | None = None
) -> list[Notification]:
    """Notificaciones ordenadas; opcionalmente las de una alerta."""
    statement = select(Notification).order_by(Notification.alert_id, Notification.forecast_date)
    if alert_id is not None:
        statement = statement.where(Notification.alert_id == alert_id)
    result = await session.execute(statement)
    return list(result.scalars().all())


def _compiled_sql(*, literal_binds: bool = False) -> str:
    """SQL que Postgres recibe para una corrida (dialecto postgresql)."""
    options = {"literal_binds": True} if literal_binds else {}
    return str(
        _build_statement(TODAY).compile(dialect=postgresql.dialect(), compile_kwargs=options)
    )


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
    """El INSERT va en un CTE y el conteo lo hace Postgres: nunca se traen las filas a
    Python (`len(result.all())`)."""
    sql = _compiled_sql()

    assert sql.startswith("WITH ins AS")
    assert "INSERT INTO notifications" in sql
    assert "ON CONFLICT (alert_id, forecast_date) DO NOTHING" in sql
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

    created = await evaluate_alerts(db_session, today=TODAY)

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

    created = await evaluate_alerts(db_session, today=TODAY)

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

    created = await evaluate_alerts(db_session, today=TODAY)

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

    created = await evaluate_alerts(db_session, today=TODAY)

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

    created = await evaluate_alerts(db_session, today=TODAY)

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

    created = await evaluate_alerts(db_session, today=TODAY)

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

    created = await evaluate_alerts(db_session, today=TODAY)

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

    created = await evaluate_alerts(db_session, today=TODAY)

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

    first = await evaluate_alerts(db_session, today=TODAY)
    second = await evaluate_alerts(db_session, today=TODAY)

    assert (first, second) == (1, 0)
    assert len(await _notifications(db_session)) == 1


async def test_forecast_changes_after_notification_do_not_renotify(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """D1: una vez por (alerta, día), aunque la probabilidad baje y vuelva a subir."""
    field = await make_field()
    await make_alert(field, event_type=EventType.FROST, threshold=70)
    forecast = await make_forecast(
        field, forecast_date=TODAY, event_type=EventType.FROST, probability=80
    )

    assert await evaluate_alerts(db_session, today=TODAY) == 1

    forecast.probability = 10  # el pronóstico baja
    await db_session.flush()
    assert await evaluate_alerts(db_session, today=TODAY) == 0

    forecast.probability = 95  # y vuelve a subir (upsert de la ingesta)
    await db_session.flush()
    assert await evaluate_alerts(db_session, today=TODAY) == 0

    [notification] = await _notifications(db_session)
    # El snapshot conserva lo que disparó la alerta, no el pronóstico vigente.
    assert notification.probability == 80
    assert notification.threshold == 70


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

    created = await evaluate_alerts(db_session, today=TODAY)

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

    created = await evaluate_alerts(db_session, today=TODAY)

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

    assert await evaluate_alerts(db_session, today=TODAY) == 1
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

    created = await evaluate_alerts(db_session, today=TODAY)

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

    assert await evaluate_alerts(db_session, today=TODAY) == 0
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

    assert await evaluate_alerts(db_session, today=TODAY) == 0
    assert await _notifications(db_session) == []


async def test_no_forecasts_returns_zero(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    field = await make_field()
    await make_alert(field, event_type=EventType.FROST, threshold=1)

    assert await evaluate_alerts(db_session, today=TODAY) == 0
    assert await _notifications(db_session) == []


# --- "Hoy" en los bordes de mes / año -------------------------------------


@pytest.mark.parametrize("today", [date(2026, 12, 31), date(2027, 1, 1)])
async def test_today_at_month_and_year_boundary(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
    today: date,
) -> None:
    """El corte `forecast_date >= today` es correcto cruzando fin de mes y de año."""
    field = await make_field()
    await make_alert(field, event_type=EventType.FROST, threshold=50)
    for offset in (-1, 0, 1):
        await make_forecast(
            field,
            forecast_date=today + timedelta(days=offset),
            event_type=EventType.FROST,
            probability=80,
        )

    created = await evaluate_alerts(db_session, today=today)

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

    assert await evaluate_alerts(db_session, today=TODAY) == 0

    forecast.probability = 85  # la ingesta actualiza el pronóstico (upsert)
    await db_session.flush()

    assert await evaluate_alerts(db_session, today=TODAY) == 1
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

    assert await evaluate_alerts(db_session, today=TODAY) == 2

    alert.threshold = 50
    await db_session.flush()

    # Solo el día 0, que ahora cumple 50 >= 50; los días 1 y 2 siguen con su snapshot.
    assert await evaluate_alerts(db_session, today=TODAY) == 1
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

    assert await evaluate_alerts(db_session, today=TODAY) == 2

    alert.is_active = False
    await db_session.flush()
    # Un día nuevo llega mientras la alerta está pausada.
    await make_forecast(
        field, forecast_date=TODAY + timedelta(days=2), event_type=EventType.FROST, probability=80
    )
    assert await evaluate_alerts(db_session, today=TODAY) == 0
    assert len(await _notifications(db_session, alert_id=alert.id)) == 2

    alert.is_active = True
    await db_session.flush()

    assert await evaluate_alerts(db_session, today=TODAY) == 1
    assert await evaluate_alerts(db_session, today=TODAY) == 0
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

    created = await evaluate_alerts(db_session, today=TODAY)

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
    assert await evaluate_alerts(db_session, today=TODAY) == 0


async def test_reactivation_after_a_cancelled_pending_does_not_recreate_the_day(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """Comportamiento observado (posible deuda de producto, QA-1).

    Al pausar la alerta, el dispatcher cancela la notificación pendiente; al reactivarla, el
    dedupe `UNIQUE (alert_id, forecast_date)` la deja cancelada y el evaluador NO crea un
    reemplazo para ese día. El día queda sin aviso aunque la alerta vuelva a estar activa.
    """
    field = await make_field()
    alert = await make_alert(field, event_type=EventType.FROST, threshold=60)
    await make_forecast(field, forecast_date=TODAY, event_type=EventType.FROST, probability=80)
    assert await evaluate_alerts(db_session, today=TODAY) == 1
    [notification] = await _notifications(db_session, alert_id=alert.id)

    # Pausa + lo que hace el dispatcher en el claim (D12): pending -> cancelled.
    alert.is_active = False
    notification.status = NotificationStatus.CANCELLED
    notification.last_error = "alert_inactive"
    await db_session.flush()
    assert await evaluate_alerts(db_session, today=TODAY) == 0

    alert.is_active = True
    await db_session.flush()
    assert await evaluate_alerts(db_session, today=TODAY) == 0

    [still_cancelled] = await _notifications(db_session, alert_id=alert.id)
    assert still_cancelled.id == notification.id
    assert still_cancelled.status == NotificationStatus.CANCELLED
    assert still_cancelled.last_error == "alert_inactive"


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

    assert await evaluate_alerts(db_session, today=TODAY) == 2
    assert len(await _notifications(db_session)) == 2

    alert.deleted_at = FIXED_AT
    await db_session.flush()
    # El borrado lógico no toca las notificaciones y el evaluador ya no evalúa la alerta.
    assert len(await _notifications(db_session)) == 2
    assert await evaluate_alerts(db_session, today=TODAY) == 0

    new_alert = await make_alert(field, event_type=EventType.FROST, threshold=60)
    assert await evaluate_alerts(db_session, today=TODAY) == 2
    notifications = await _notifications(db_session)
    assert {n.alert_id for n in notifications} == {alert.id, new_alert.id}
    assert len(notifications) == 4


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

    created = await evaluate_alerts(db_session, today=TODAY)

    assert created == 300
    notifications = await _notifications(db_session)
    assert len(notifications) == 300
    assert {(n.alert_id, n.forecast_date) for n in notifications} == expected
    # Re-ejecutar no agrega nada.
    assert await evaluate_alerts(db_session, today=TODAY) == 0


# --- Concurrencia ----------------------------------------------------------


async def _evaluate_and_commit(
    session_factory: async_sessionmaker[AsyncSession], *, today: date
) -> int:
    """Corre el evaluador en su propia conexión y commitea (libera locks del INSERT)."""
    async with session_factory() as session:
        created = await evaluate_alerts(session, today=today)
        await session.commit()
        return created


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
            _evaluate_and_commit(session_factory, today=TODAY),
            _evaluate_and_commit(session_factory, today=TODAY),
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
