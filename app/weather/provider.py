"""Proveedor meteorológico mock (simula el servicio externo).

Spec: docs/JOBS.md §Ingesta mock. El proveedor real (en producción) resolvería lat/long
contra un servicio de clima; este genera probabilidades determinísticas por campo y día.
"""

import random
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol

from app.models import EventType, Field


@dataclass(frozen=True, slots=True)
class ForecastPoint:
    """Un pronóstico puntual: probabilidad de un evento en un campo y un día."""

    field_id: int
    forecast_date: date
    event_type: EventType
    probability: int


class WeatherProvider(Protocol):
    """Contrato del proveedor de pronósticos."""

    async def fetch(self, field: Field, *, start: date, days: int) -> list[ForecastPoint]:
        """Devuelve los pronósticos de `field` para `[start, start + days)`."""
        ...


class MockWeatherProvider:
    """Proveedor determinístico: misma semilla, mismo campo y día -> misma probabilidad."""

    def __init__(self, seed: int = 42) -> None:
        self.seed = seed

    async def fetch(self, field: Field, *, start: date, days: int) -> list[ForecastPoint]:
        """Genera una probabilidad 0..100 por (día, `EventType`) usando `random.Random`.

        La semilla se compone con el id del campo, la fecha ISO y el valor del evento, así
        que el resultado no depende del orden de las consultas ni del resto del proceso.
        """
        points: list[ForecastPoint] = []
        for offset in range(days):
            day = start + timedelta(days=offset)
            for event_type in EventType:
                rng = random.Random(f"{self.seed}:{field.id}:{day.isoformat()}:{event_type.value}")
                points.append(
                    ForecastPoint(
                        field_id=field.id,
                        forecast_date=day,
                        event_type=event_type,
                        probability=rng.randint(0, 100),
                    )
                )
        return points
