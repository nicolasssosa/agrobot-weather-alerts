"""Schemas Pydantic v2 de `forecast`. Ver docs/API.md §forecasts."""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict

from app.models import EventType


class ForecastRead(BaseModel):
    """Un pronóstico expuesto por la API (sin ids ni timestamps internos)."""

    model_config = ConfigDict(from_attributes=True)

    forecast_date: date
    event_type: EventType
    probability: int
    source: str
    fetched_at: datetime
