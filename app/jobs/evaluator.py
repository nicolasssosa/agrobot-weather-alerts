"""Evaluación de alertas vs. pronósticos almacenados.

Spec: docs/JOBS.md §Evaluador. Una sola sentencia set-based: inserta en `notifications` las
combinaciones (alerta, día) cuyo pronóstico alcanza el umbral, con
`ON CONFLICT (alert_id, forecast_date) DO NOTHING`, así que re-ejecutar (o correr N workers)
nunca duplica. El insert va envuelto en un CTE y el conteo se hace en SQL (`SELECT count(*)`),
así no se materializan en Python las filas del `RETURNING`. Es el paso "evaluar" del ciclo del
worker; no commitea.

Desde S11 (D14) sólo se evalúan pronósticos frescos: `issued_at >= now - MAX_FORECAST_AGE_HOURS`.
Además, un `issued_at` en el futuro más allá de `FORECAST_FUTURE_TOLERANCE_MINUTES` tampoco se
evalúa (defensa ante datos inválidos que la ingesta debió acotar). Los pronósticos que quedaron
afuera por viejos se cuentan con una consulta agregada aparte y, si hay alguno, se loguea un
warning (señal de que la ingesta no trae datos frescos).
"""

import logging
from datetime import date, datetime, timedelta

from sqlalchemy import Select, func, literal, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Alert, Notification, NotificationStatus, WeatherForecast

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


def _freshness_cutoff(*, now: datetime, max_forecast_age_hours: int) -> datetime:
    """Instante mínimo de emisión aceptado: `now - max_forecast_age_hours`."""
    return now - timedelta(hours=max_forecast_age_hours)


def _future_limit(*, now: datetime, forecast_future_tolerance_minutes: int) -> datetime:
    """Instante máximo de emisión aceptado: `now + forecast_future_tolerance_minutes`."""
    return now + timedelta(minutes=forecast_future_tolerance_minutes)


def _build_statement(
    today: date,
    *,
    now: datetime,
    max_forecast_age_hours: int,
    forecast_future_tolerance_minutes: int,
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
        .join(
            WeatherForecast,
            (WeatherForecast.field_id == Alert.field_id)
            & (WeatherForecast.event_type == Alert.event_type),
        )
        .where(
            # Predicado igual al del índice parcial (S9):
            # `ix_alerts_active_field_event ... WHERE is_active AND deleted_at IS NULL`.
            # Boolean bare (no `IS true`) para que el planner pueda usar el índice.
            Alert.is_active,
            Alert.deleted_at.is_(None),
            WeatherForecast.forecast_date >= today,
            WeatherForecast.probability >= Alert.threshold,
            # Frescura (S11, D14): borde incluido, `== cutoff` se evalúa.
            WeatherForecast.issued_at
            >= _freshness_cutoff(now=now, max_forecast_age_hours=max_forecast_age_hours),
            # Defensa ante un `issued_at` futuro que la ingesta no acotó (S11 SEC-1).
            WeatherForecast.issued_at
            <= _future_limit(
                now=now, forecast_future_tolerance_minutes=forecast_future_tolerance_minutes
            ),
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


def _build_stale_statement(
    today: date, *, now: datetime, max_forecast_age_hours: int
) -> Select[tuple[int, int]]:
    """Cuenta `(pronósticos viejos, campos distintos)` que el evaluador descartó por frescura.

    Mismas condiciones que el INSERT (alertas activas y no borradas, mismo campo y evento,
    `forecast_date >= today` y `probability >= threshold`) salvo la frescura: acá interesan los
    pronósticos de la ventana que habrían disparado alguna alerta y quedaron afuera por viejos.
    El `count` es de pronósticos **distintos**: el fan-out del JOIN con varias alertas del mismo
    (campo, evento) no lo infla (S11 QA-1/CR-3). Agregado en SQL: no se traen filas.
    """
    statement = (
        select(
            func.count(func.distinct(WeatherForecast.id)),
            func.count(func.distinct(WeatherForecast.field_id)),
        )
        .select_from(Alert)
        .join(
            WeatherForecast,
            (WeatherForecast.field_id == Alert.field_id)
            & (WeatherForecast.event_type == Alert.event_type),
        )
        .where(
            Alert.is_active,
            Alert.deleted_at.is_(None),
            WeatherForecast.forecast_date >= today,
            WeatherForecast.probability >= Alert.threshold,
            WeatherForecast.issued_at
            < _freshness_cutoff(now=now, max_forecast_age_hours=max_forecast_age_hours),
        )
    )
    return statement


async def evaluate_alerts(
    session: AsyncSession,
    *,
    today: date,
    now: datetime,
    max_forecast_age_hours: int,
    forecast_future_tolerance_minutes: int,
) -> int:
    """Crea las notificaciones pendientes que disparan las alertas activas.

    Matchea por campo y tipo de evento, solo días `>= today` (sin horizonte, D2), probabilidad
    `>= threshold` y pronósticos emitidos dentro de la ventana de frescura
    (`issued_at >= now - max_forecast_age_hours`, borde incluido). Un `issued_at` posterior a
    `now + forecast_future_tolerance_minutes` tampoco se evalúa (S11 SEC-1). Una notificación
    por (alerta, día): el `ON CONFLICT DO NOTHING` hace la operación idempotente (D1).

    Si hay pronósticos de la ventana descartados por viejos, loguea
    `stale_forecasts count=<n> fields=<m> max_age_hours=<h>` (S11, D14), contando pronósticos
    distintos que habrían disparado alguna alerta activa.

    No commitea: el llamador es dueño de la transacción. Devuelve la cantidad de
    notificaciones nuevas, contada por Postgres dentro de la misma sentencia (sin
    materializar el `RETURNING` en Python).
    """
    created: int = (
        await session.execute(
            _build_statement(
                today,
                now=now,
                max_forecast_age_hours=max_forecast_age_hours,
                forecast_future_tolerance_minutes=forecast_future_tolerance_minutes,
            )
        )
    ).scalar_one()
    logger.info("alerts_evaluated created=%d today=%s", created, today)

    stale_count, stale_fields = (
        await session.execute(
            _build_stale_statement(today, now=now, max_forecast_age_hours=max_forecast_age_hours)
        )
    ).one()
    if stale_count:
        logger.warning(
            "stale_forecasts count=%d fields=%d max_age_hours=%d",
            stale_count,
            stale_fields,
            max_forecast_age_hours,
        )
    return created
