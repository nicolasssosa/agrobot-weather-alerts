"""Lectura de pronósticos (solo lectura; los escribe el job de ingesta). TODO(S3).

async def list_forecasts(
    session, user_id: int, field_id: int, *,
    date_from: date | None, date_to: date | None, event_type: EventType | None,
) -> Sequence[WeatherForecast]
    - Valida ownership con fields.get_field (NotFoundError si no corresponde).
    - date_from > date_to -> lo valida el router (422) antes de llegar acá.
    - Orden: forecast_date ASC, event_type ASC.
"""
