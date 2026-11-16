"""Evaluación de alertas vs. pronósticos almacenados.

Spec: docs/JOBS.md §Evaluador. Una sola sentencia set-based: inserta en `notifications` las
combinaciones (alerta, día) cuyo pronóstico alcanza el umbral, con
`ON CONFLICT (alert_id, forecast_date) DO NOTHING`, así que re-ejecutar (o correr N workers)
nunca duplica. El insert va envuelto en un CTE y el conteo se hace en SQL (`SELECT count(*)`),
así no se materializan en Python las filas del `RETURNING`. Es el paso "evaluar" del ciclo del
worker; no commitea.
"""

import logging
from datetime import date

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


def _build_statement(today: date) -> Select[tuple[int]]:
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
            # Boolean bare (no `IS true`): habilita el índice parcial
            # `ix_alerts_active_field_event ... WHERE is_active`.
            Alert.is_active,
            WeatherForecast.forecast_date >= today,
            WeatherForecast.probability >= Alert.threshold,
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


async def evaluate_alerts(session: AsyncSession, *, today: date) -> int:
    """Crea las notificaciones pendientes que disparan las alertas activas.

    Matchea por campo y tipo de evento, solo días `>= today` (sin horizonte, D2) y
    probabilidad `>= threshold`. Una notificación por (alerta, día): el
    `ON CONFLICT DO NOTHING` hace la operación idempotente (D1).

    No commitea: el llamador es dueño de la transacción. Devuelve la cantidad de
    notificaciones nuevas, contada por Postgres dentro de la misma sentencia (sin
    materializar el `RETURNING` en Python).
    """
    created: int = (await session.execute(_build_statement(today))).scalar_one()
    logger.info("alerts_evaluated created=%d today=%s", created, today)
    return created
