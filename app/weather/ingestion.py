"""Ingesta de pronósticos (simula el job existente del sistema). TODO(S3).

async def ingest_forecasts(session: AsyncSession, provider: WeatherProvider, *,
                           start: date, days: int, source: str = "mock") -> int
    - Recorre todos los fields (en batches con `yield_per` / paginado por id).
    - Upsert: INSERT ... ON CONFLICT (field_id, forecast_date, event_type)
      DO UPDATE SET probability, source, fetched_at = now(), updated_at = now().
    - Devuelve la cantidad de filas upserteadas. NO commitea.
"""
