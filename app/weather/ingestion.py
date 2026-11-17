"""Ingesta de pronósticos (simula el job existente del sistema).

Spec: docs/JOBS.md §Ingesta mock. Recorre los campos paginados por id y hace un upsert
set-based por página: re-ejecutar la ingesta actualiza las probabilidades, nunca duplica.
El upsert es **condicional** (S11, D14): un dato igual o más viejo que el guardado no lo pisa,
así un lote atrasado no retrocede el pronóstico vigente.

Antes del upsert se valida `issued_at`: un punto sin zona horaria se descarta (no aborta el
lote) y uno emitido en el futuro más allá de la tolerancia se acota a `now`, así una corrida
posterior válida lo puede pisar (S11 SEC-1/SEC-2).
"""

import logging
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Field, WeatherForecast
from app.weather.provider import WeatherProvider

logger = logging.getLogger(__name__)

#: Tamaño de página del keyset sobre `fields.id` (docs/JOBS.md §Ingesta mock).
_BATCH_SIZE = 500

#: Tope de parámetros bind por statement (asyncpg rechaza más de 32767). Con margen.
_MAX_PARAMS = 32_000

#: Tolerancia por defecto para un `issued_at` en el futuro (`FORECAST_FUTURE_TOLERANCE_MINUTES`).
DEFAULT_FUTURE_TOLERANCE_MINUTES = 60


async def _list_fields_page(
    session: AsyncSession, *, last_id: int, field_ids: Sequence[int] | None = None
) -> Sequence[Field]:
    """Siguiente página de campos por id (keyset: `id > last_id`, sin OFFSET).

    Con `field_ids`, la misma paginación keyset se restringe a esos campos.
    """
    statement = select(Field).where(Field.id > last_id)
    if field_ids is not None:
        statement = statement.where(Field.id.in_(field_ids))
    result = await session.execute(statement.order_by(Field.id).limit(_BATCH_SIZE))
    return result.scalars().all()


async def _upsert_chunk(session: AsyncSession, rows: Sequence[dict[str, object]]) -> int:
    """Upsert set-based de un chunk; devuelve las filas realmente escritas.

    `ON CONFLICT ... DO UPDATE ... WHERE issued_at < excluded.issued_at` sólo devuelve en el
    `RETURNING` las filas insertadas o actualizadas: las descartadas por más viejas (o iguales)
    no se cuentan. El `count(*)` lo hace Postgres dentro del mismo statement (CTE), sin
    materializar las filas en Python.
    """
    statement = pg_insert(WeatherForecast).values(rows)
    upsert = statement.on_conflict_do_update(
        index_elements=["field_id", "forecast_date", "event_type"],
        set_={
            "probability": statement.excluded.probability,
            "source": statement.excluded.source,
            "issued_at": statement.excluded.issued_at,
            "fetched_at": func.now(),
            "updated_at": func.now(),
        },
        where=WeatherForecast.issued_at < statement.excluded.issued_at,
    ).returning(WeatherForecast.id)
    written = upsert.cte("written")
    return (await session.execute(select(func.count()).select_from(written))).scalar_one()


async def _upsert_page(
    session: AsyncSession,
    provider: WeatherProvider,
    fields: Sequence[Field],
    *,
    start: date,
    days: int,
    source: str,
    now: datetime,
    future_tolerance_minutes: int,
) -> tuple[int, int, int, int]:
    """Upsertea una página y devuelve `(escritas, intentadas, inválidas, futuras)`.

    `intentadas` cuenta solo los puntos válidos: los descartados por `issued_at` inválido se
    informan aparte y no se confunden con `forecasts_skipped_older`.
    """
    rows: list[dict[str, object]] = []
    invalid_issued_at = 0
    issued_in_future = 0
    future_limit = now + timedelta(minutes=future_tolerance_minutes)
    for field in fields:
        for point in await provider.fetch(field, start=start, days=days):
            issued_at = point.issued_at
            if issued_at is None or issued_at.tzinfo is None or issued_at.utcoffset() is None:
                invalid_issued_at += 1
                continue
            normalized_issued_at = issued_at.astimezone(UTC)
            if normalized_issued_at > future_limit:
                # Se acota a `now` para que una corrida posterior válida lo pueda pisar.
                normalized_issued_at = now
                issued_in_future += 1
            rows.append(
                {
                    "field_id": point.field_id,
                    "forecast_date": point.forecast_date,
                    "event_type": point.event_type,
                    "probability": point.probability,
                    "source": source,
                    "issued_at": normalized_issued_at,
                    "fetched_at": func.now(),
                }
            )
    if not rows:
        return (0, 0, invalid_issued_at, issued_in_future)

    # Trocea las filas para no superar el límite de parámetros de asyncpg: una página
    # completa (500 campos x 14 días x 5 eventos) arma 35000 filas de una sola vez.
    # `len(rows[0])` es un tope conservador de columnas bindeadas (`fetched_at` se
    # resuelve a `now()` en SQL, no como parámetro).
    chunk_size = max(1, _MAX_PARAMS // len(rows[0]))
    written = 0
    for offset in range(0, len(rows), chunk_size):
        written += await _upsert_chunk(session, rows[offset : offset + chunk_size])
    return (written, len(rows), invalid_issued_at, issued_in_future)


async def ingest_forecasts(
    session: AsyncSession,
    provider: WeatherProvider,
    *,
    start: date,
    days: int,
    source: str = "mock",
    field_ids: Sequence[int] | None = None,
    now: datetime | None = None,
    future_tolerance_minutes: int = DEFAULT_FUTURE_TOLERANCE_MINUTES,
) -> int:
    """Upsertea los pronósticos para `[start, start + days)`.

    Sin `field_ids` procesa todos los campos; si se pasa, solo esos (misma paginación keyset).
    `now` es la corrida de la ingesta (inyectable; por defecto, la hora real en UTC) y define
    el tope de un `issued_at` en el futuro (`now + future_tolerance_minutes`), que se acota a
    `now`. Un `issued_at` `None` o sin zona horaria descarta ese punto sin abortar el lote.
    No commitea: el caller (la API o el script) es dueño de la transacción.
    Devuelve la cantidad de filas **escritas** (insertadas o actualizadas); las que se
    descartan por ser iguales o más viejas que el pronóstico guardado se loguean como
    `forecasts_skipped_older` (S11, D14).
    """
    run_now = now if now is not None else datetime.now(UTC)
    last_id = 0
    written = 0
    attempted = 0
    invalid_issued_at = 0
    issued_in_future = 0
    while True:
        fields = await _list_fields_page(session, last_id=last_id, field_ids=field_ids)
        if not fields:
            break
        page_written, page_attempted, page_invalid, page_future = await _upsert_page(
            session,
            provider,
            fields,
            start=start,
            days=days,
            source=source,
            now=run_now,
            future_tolerance_minutes=future_tolerance_minutes,
        )
        written += page_written
        attempted += page_attempted
        invalid_issued_at += page_invalid
        issued_in_future += page_future
        last_id = fields[-1].id
        if len(fields) < _BATCH_SIZE:
            break
    if invalid_issued_at:
        logger.warning("forecast_invalid_issued_at count=%d", invalid_issued_at)
    if issued_in_future:
        logger.warning("forecast_issued_in_future count=%d", issued_in_future)
    skipped = attempted - written
    if skipped:
        logger.info("forecasts_skipped_older count=%d", skipped)
    return written
