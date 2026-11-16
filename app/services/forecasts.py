"""Lectura de pronósticos (solo lectura; los escribe el job de ingesta)."""

from collections.abc import Sequence
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EventType, WeatherForecast
from app.services.fields import get_field


async def list_forecasts(
    session: AsyncSession,
    user_id: int,
    field_id: int,
    *,
    date_from: date | None,
    date_to: date | None,
    event_type: EventType | None,
) -> Sequence[WeatherForecast]:
    """Lista los pronósticos de un campo del usuario, ordenados por fecha y evento.

    Valida ownership con `get_field` (404 si el campo no existe o es de otro usuario).
    El rango `date_from <= date_to` lo valida el router antes de llegar acá.
    """
    await get_field(session, user_id, field_id)

    query = select(WeatherForecast).where(WeatherForecast.field_id == field_id)
    if date_from is not None:
        query = query.where(WeatherForecast.forecast_date >= date_from)
    if date_to is not None:
        query = query.where(WeatherForecast.forecast_date <= date_to)
    if event_type is not None:
        query = query.where(WeatherForecast.event_type == event_type)
    query = query.order_by(WeatherForecast.forecast_date, WeatherForecast.event_type)

    result = await session.execute(query)
    return result.scalars().all()
