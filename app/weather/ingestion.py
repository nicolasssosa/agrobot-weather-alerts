"""Ingesta de pronósticos (simula el job existente del sistema).

Spec: docs/JOBS.md §Ingesta mock. Recorre los campos paginados por id y hace un upsert
set-based por página: re-ejecutar la ingesta actualiza las probabilidades, nunca duplica.
"""

from collections.abc import Sequence
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Field, WeatherForecast
from app.weather.provider import WeatherProvider

#: Tamaño de página del keyset sobre `fields.id` (docs/JOBS.md §Ingesta mock).
_BATCH_SIZE = 500

#: Tope de parámetros bind por statement (asyncpg rechaza más de 32767). Con margen.
_MAX_PARAMS = 32_000


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


async def _upsert_chunk(session: AsyncSession, rows: Sequence[dict[str, object]]) -> None:
    """Upsert set-based de un chunk de filas; misma semántica para todos los chunks."""
    statement = pg_insert(WeatherForecast).values(rows)
    upsert = statement.on_conflict_do_update(
        index_elements=["field_id", "forecast_date", "event_type"],
        set_={
            "probability": statement.excluded.probability,
            "source": statement.excluded.source,
            "fetched_at": func.now(),
            "updated_at": func.now(),
        },
    )
    await session.execute(upsert)


async def _upsert_page(
    session: AsyncSession,
    provider: WeatherProvider,
    fields: Sequence[Field],
    *,
    start: date,
    days: int,
    source: str,
) -> int:
    """Hace el upsert de los pronósticos de una página de campos y devuelve las filas."""
    rows: list[dict[str, object]] = []
    for field in fields:
        for point in await provider.fetch(field, start=start, days=days):
            rows.append(
                {
                    "field_id": point.field_id,
                    "forecast_date": point.forecast_date,
                    "event_type": point.event_type,
                    "probability": point.probability,
                    "source": source,
                    "fetched_at": func.now(),
                }
            )
    if not rows:
        return 0

    # Trocea las filas para no superar el límite de parámetros de asyncpg: una página
    # completa (500 campos x 14 días x 5 eventos) arma 35000 filas de una sola vez.
    # `len(rows[0])` es un tope conservador de columnas bindeadas (`fetched_at` se
    # resuelve a `now()` en SQL, no como parámetro).
    chunk_size = max(1, _MAX_PARAMS // len(rows[0]))
    for offset in range(0, len(rows), chunk_size):
        await _upsert_chunk(session, rows[offset : offset + chunk_size])
    return len(rows)


async def ingest_forecasts(
    session: AsyncSession,
    provider: WeatherProvider,
    *,
    start: date,
    days: int,
    source: str = "mock",
    field_ids: Sequence[int] | None = None,
) -> int:
    """Upsertea los pronósticos para `[start, start + days)`.

    Sin `field_ids` procesa todos los campos; si se pasa, solo esos (misma paginación keyset).
    No commitea: el caller (la API o el script) es dueño de la transacción.
    Devuelve la cantidad de filas procesadas.
    """
    last_id = 0
    processed = 0
    while True:
        fields = await _list_fields_page(session, last_id=last_id, field_ids=field_ids)
        if not fields:
            break
        processed += await _upsert_page(
            session, provider, fields, start=start, days=days, source=source
        )
        last_id = fields[-1].id
        if len(fields) < _BATCH_SIZE:
            break
    return processed
