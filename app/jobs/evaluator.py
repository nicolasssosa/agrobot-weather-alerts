"""Evaluación de alertas vs. pronósticos almacenados.

Spec: docs/JOBS.md §Evaluador. Una sola sentencia set-based: inserta en `notifications` las
combinaciones (alerta, día) cuyo pronóstico alcanza el umbral, con
`ON CONFLICT (alert_id, forecast_date) DO NOTHING`, así que re-ejecutar (o correr N workers)
nunca duplica. El insert va envuelto en un CTE y el conteo se hace en SQL (`SELECT count(*)`),
así no se materializan en Python las filas del `RETURNING`. Es el paso "evaluar" del ciclo del
worker; no commitea.

Desde S12a (D9) el "hoy" es **local de cada campo**: `(:now AT TIME ZONE fields.timezone)::date`,
con `now` un `TIMESTAMPTZ` en UTC. Se evalúa `[hoy_local, hoy_local + horizon_days)`; el
horizonte (`ALERT_HORIZON_DAYS`, D2) de 0 significa sin límite.

El bound local (`AT TIME ZONE`) no es un valor constante, así que por sí solo no puede usarse
como condición de índice sobre `UNIQUE (field_id, forecast_date, event_type)`. Por eso se suma
un **prefiltro constante** (S12a CR-2) derivado de `now` en UTC: `forecast_date >= hoy_utc - 1`
y, con horizonte, `forecast_date <= hoy_utc + horizon_days + 1`. Es un superconjunto holgado
(los offsets del mundo están entre -12 y +14 h) y los bounds locales exactos siguen filtrando.

Desde S11 (D14) sólo se evalúan pronósticos frescos: `issued_at >= now - MAX_FORECAST_AGE_HOURS`.
Además, un `issued_at` en el futuro más allá de `FORECAST_FUTURE_TOLERANCE_MINUTES` tampoco se
evalúa (defensa ante datos inválidos que la ingesta debió acotar). Los pronósticos que quedaron
afuera por viejos o por adelantados se cuentan con **una sola** consulta agregada (`FILTER`) y,
si hay alguno, se loguea un warning (señal de que la ingesta no trae datos frescos o emite
datos inválidos).

Desde S12a CR-1/SEC-1 el evaluador sólo considera campos cuya zona existe en
`pg_timezone_names`: una zona desconocida haría fallar `AT TIME ZONE` y dejaría sin evaluar a
todos los campos. Además, la expresión del "hoy" local va envuelta en un `CASE` (CR-4/SEC-1)
porque el `IN` del `WHERE` no garantiza el orden de evaluación: el planner puede calcular
`AT TIME ZONE` antes de descartar la fila. Los campos salteados se cuentan y se loguea
`invalid_timezone_fields count=<n>`.
"""

import logging
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import (
    ColumnElement,
    Date,
    DateTime,
    Integer,
    Select,
    case,
    func,
    literal,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnExpressionArgument

from app.models import Alert, Field, Notification, NotificationStatus, WeatherForecast
from app.services.timezones import PG_TIMEZONE_NAMES

logger = logging.getLogger(__name__)

#: Columnas del INSERT ... SELECT, en el mismo orden que el `select` de abajo.
_INSERT_COLUMNS = (
    "alert_id",
    "forecast_date",
    "event_type",
    "probability",
    "threshold",
    "status",
    "next_attempt_at",
)


def _utc_today(now: datetime) -> date:
    """Fecha de `now` en UTC: la base del prefiltro constante (S12a CR-2)."""
    return now.astimezone(UTC).date()


def _local_today(now: datetime) -> ColumnElement[date]:
    """Expresión SQL del "hoy" local del campo, protegida con `CASE` (S12a CR-4/SEC-1).

    `CASE WHEN fields.timezone IN (SELECT name FROM pg_timezone_names)
          THEN (:now AT TIME ZONE fields.timezone)::date END`

    El `IN` del `WHERE` no alcanza: Postgres no garantiza el orden de evaluación y puede
    calcular `AT TIME ZONE` antes de descartar la fila (por ejemplo como filtro de un hash
    join), y una zona desconocida aborta la sentencia entera. El `CASE` sí impone el orden
    por fila: con una zona desconocida el resultado es `NULL` (el `ELSE` implícito), la
    comparación da falso y el resto de los campos se evalúa igual.
    """
    return case(
        (
            _known_timezone(),
            literal(now, type_=DateTime(timezone=True))
            .op("AT TIME ZONE")(Field.timezone)
            .cast(Date),
        )
    )


def _started_window(
    now: datetime, horizon_days: int
) -> tuple[ColumnElement[date], ColumnElement[bool]]:
    """Condiciones de la ventana de días evaluada, relativas al "hoy" local del campo.

    `horizon_days = 0` (D2) significa sin límite superior: solo cuenta `forecast_date >= hoy`.
    """
    today = _local_today(now)
    return today, or_(
        literal(horizon_days, Integer) == 0,
        WeatherForecast.forecast_date < today + literal(horizon_days, Integer),
    )


def _freshness_cutoff(*, now: datetime, max_forecast_age_hours: int) -> datetime:
    """Instante mínimo de emisión aceptado: `now - max_forecast_age_hours`."""
    return now - timedelta(hours=max_forecast_age_hours)


def _future_limit(*, now: datetime, future_tolerance_minutes: int) -> datetime:
    """Instante máximo de emisión aceptado: `now + future_tolerance_minutes`."""
    return now + timedelta(minutes=future_tolerance_minutes)


def _date_floor(now: datetime) -> ColumnElement[date]:
    """Prefiltro constante `forecast_date >= (:now)::date - 1` (S12a CR-2).

    La fecha se calcula en Python **en UTC** y se bindea como `DATE`: así el rango no depende
    de la zona horaria de la sesión de Postgres y es una condición de índice.
    """
    return literal(_utc_today(now) - timedelta(days=1), type_=Date())


def _date_ceiling(now: datetime, horizon_days: int) -> ColumnElement[date]:
    """Prefiltro constante `forecast_date <= (:now)::date + horizon_days + 1` (S12a CR-2).

    Sólo aplica con horizonte; es un superconjunto del bound local exacto.
    """
    return literal(_utc_today(now) + timedelta(days=horizon_days + 1), type_=Date())


def _known_timezone() -> ColumnElement[bool]:
    """`fields.timezone IN (SELECT name FROM pg_timezone_names)` (S12a CR-1/SEC-1)."""
    return Field.timezone.in_(select(PG_TIMEZONE_NAMES.c.name))


def _alert_conditions(*, now: datetime, horizon_days: int) -> list[ColumnExpressionArgument[bool]]:
    """Predicados comunes al INSERT y a los warnings.

    Alertas activas y no borradas (el predicado del índice parcial, S9), zona conocida por
    Postgres, prefiltro constante de fechas (CR-2), ventana local exacta (D9/D2) y umbral.
    """
    today, in_horizon = _started_window(now, horizon_days)
    conditions: list[ColumnExpressionArgument[bool]] = [
        # Boolean bare (no `IS true`) para que el planner pueda usar el índice parcial.
        Alert.is_active,
        Alert.deleted_at.is_(None),
        _known_timezone(),
        WeatherForecast.forecast_date >= _date_floor(now),
        WeatherForecast.forecast_date >= today,
        in_horizon,
        WeatherForecast.probability >= Alert.threshold,
    ]
    if horizon_days > 0:
        conditions.append(WeatherForecast.forecast_date <= _date_ceiling(now, horizon_days))
    return conditions


def _build_statement(
    *,
    now: datetime,
    horizon_days: int,
    max_forecast_age_hours: int,
    future_tolerance_minutes: int,
) -> Select[tuple[int]]:
    """Arma el `WITH ins AS (INSERT ...) SELECT count(*) FROM ins` de una corrida."""
    matching = (
        select(
            Alert.id,
            WeatherForecast.forecast_date,
            WeatherForecast.event_type,
            WeatherForecast.probability,
            Alert.threshold,
            literal(NotificationStatus.PENDING.value),
            func.now(),
        )
        .select_from(Alert)
        .join(Field, Field.id == Alert.field_id)
        .join(
            WeatherForecast,
            (WeatherForecast.field_id == Alert.field_id)
            & (WeatherForecast.event_type == Alert.event_type),
        )
        .where(
            *_alert_conditions(now=now, horizon_days=horizon_days),
            # Frescura (S11, D14): borde incluido, `== cutoff` se evalúa.
            WeatherForecast.issued_at
            >= _freshness_cutoff(now=now, max_forecast_age_hours=max_forecast_age_hours),
            # Defensa ante un `issued_at` futuro que la ingesta no acotó (S11 SEC-1).
            WeatherForecast.issued_at
            <= _future_limit(now=now, future_tolerance_minutes=future_tolerance_minutes),
        )
    )
    inserted = (
        pg_insert(Notification)
        .from_select(_INSERT_COLUMNS, matching, include_defaults=False)
        .on_conflict_do_nothing(index_elements=["alert_id", "forecast_date"])
        .returning(Notification.id)
        .cte("ins")
    )
    return select(func.count()).select_from(inserted)


def _build_out_of_window_statement(
    *,
    now: datetime,
    horizon_days: int,
    max_forecast_age_hours: int,
    future_tolerance_minutes: int,
) -> Select[tuple[int, int, int, int]]:
    """Cuenta en **una** consulta los pronósticos descartados por viejos y por adelantados.

    Mismas condiciones que el INSERT (alertas activas y no borradas, zona conocida, prefiltro
    y ventana de días, `probability >= threshold`) salvo el corte de `issued_at`, que se aplica
    con agregados `FILTER (WHERE ...)`: `stale` = `issued_at < now - max_age`,
    `future` = `issued_at > now + tolerance`. Devuelve
    `(pronósticos viejos, campos viejos, pronósticos adelantados, campos adelantados)`.

    El `count` es de pronósticos **distintos**: el fan-out del JOIN con varias alertas del
    mismo (campo, evento) no lo infla (S11 QA-1/CR-3). Agregado en SQL: no se traen filas.
    """
    stale = WeatherForecast.issued_at < _freshness_cutoff(
        now=now, max_forecast_age_hours=max_forecast_age_hours
    )
    ahead = WeatherForecast.issued_at > _future_limit(
        now=now, future_tolerance_minutes=future_tolerance_minutes
    )
    return (
        select(
            func.count(func.distinct(WeatherForecast.id)).filter(stale),
            func.count(func.distinct(WeatherForecast.field_id)).filter(stale),
            func.count(func.distinct(WeatherForecast.id)).filter(ahead),
            func.count(func.distinct(WeatherForecast.field_id)).filter(ahead),
        )
        .select_from(Alert)
        .join(Field, Field.id == Alert.field_id)
        .join(
            WeatherForecast,
            (WeatherForecast.field_id == Alert.field_id)
            & (WeatherForecast.event_type == Alert.event_type),
        )
        .where(*_alert_conditions(now=now, horizon_days=horizon_days))
    )


def _build_invalid_timezone_statement() -> Select[tuple[int]]:
    """Cuenta los campos con alertas activas cuya zona Postgres no reconoce (S12a SEC-1)."""
    return (
        select(func.count(func.distinct(Field.id)))
        .select_from(Alert)
        .join(Field, Field.id == Alert.field_id)
        .where(Alert.is_active, Alert.deleted_at.is_(None), ~_known_timezone())
    )


async def evaluate_alerts(
    session: AsyncSession,
    *,
    now: datetime,
    horizon_days: int,
    max_forecast_age_hours: int,
    future_tolerance_minutes: int,
) -> int:
    """Crea las notificaciones pendientes que disparan las alertas activas.

    "Hoy" es la fecha **local de cada campo** (`fields.timezone`, D9): con `now` a las
    01:00 UTC del 17, un campo en Buenos Aires sigue en el 16. Se evalúan los días
    `[hoy_local, hoy_local + horizon_days)`; `horizon_days = 0` (D2) no pone límite superior.

    Los campos cuya zona no existe en `pg_timezone_names` se saltean (una zona desconocida
    rompería `AT TIME ZONE` para todos) y se countan en `invalid_timezone_fields` (S12a SEC-1).

    Matchea por campo y tipo de evento, probabilidad `>= threshold` y pronósticos emitidos
    dentro de la ventana de frescura (`issued_at >= now - max_forecast_age_hours`, borde
    incluido). Un `issued_at` posterior a `now + future_tolerance_minutes` tampoco se evalúa
    (S11 SEC-1). Una notificación por (alerta, día): el `ON CONFLICT DO NOTHING` hace la
    operación idempotente (D1).

    Si hay pronósticos de la ventana descartados por viejos o por adelantados, loguea
    `stale_forecasts count=<n> fields=<m> max_age_hours=<h>` (S11, D14) y
    `future_forecasts count=<n> fields=<m> tolerance_minutes=<t>` (S12a SEC-4), ambos desde
    una sola consulta agregada.

    No commitea: el llamador es dueño de la transacción. Devuelve la cantidad de
    notificaciones nuevas, contada por Postgres dentro de la misma sentencia (sin
    materializar el `RETURNING` en Python).
    """
    created: int = (
        await session.execute(
            _build_statement(
                now=now,
                horizon_days=horizon_days,
                max_forecast_age_hours=max_forecast_age_hours,
                future_tolerance_minutes=future_tolerance_minutes,
            )
        )
    ).scalar_one()
    logger.info("alerts_evaluated created=%d now=%s horizon_days=%d", created, now, horizon_days)

    stale_count, stale_fields, future_count, future_fields = (
        await session.execute(
            _build_out_of_window_statement(
                now=now,
                horizon_days=horizon_days,
                max_forecast_age_hours=max_forecast_age_hours,
                future_tolerance_minutes=future_tolerance_minutes,
            )
        )
    ).one()
    if stale_count:
        logger.warning(
            "stale_forecasts count=%d fields=%d max_age_hours=%d",
            stale_count,
            stale_fields,
            max_forecast_age_hours,
        )
    if future_count:
        logger.warning(
            "future_forecasts count=%d fields=%d tolerance_minutes=%d",
            future_count,
            future_fields,
            future_tolerance_minutes,
        )

    invalid_timezones: int = (
        await session.execute(_build_invalid_timezone_statement())
    ).scalar_one()
    if invalid_timezones:
        logger.warning("invalid_timezone_fields count=%d", invalid_timezones)
    return created
