"""Proveedor meteorológico mock (simula el servicio externo).

Spec: docs/JOBS.md §Ingesta mock. El proveedor real (en producción) resolvería lat/long
contra un servicio de clima; este genera probabilidades determinísticas por campo y día.
"""

import random
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from app.models import EventType, Field


@dataclass(frozen=True, slots=True)
class ForecastPoint:
    """Un pronóstico puntual: probabilidad de un evento en un campo y un día."""

    field_id: int
    forecast_date: date
    event_type: EventType
    probability: int
    #: Corrida del modelo que lo emitió (docs/JOBS.md §Ingesta mock, S11). Un proveedor
    #: defectuoso puede entregarlo `None` o sin zona horaria; la ingesta descarta ese punto.
    issued_at: datetime | None


class WeatherProvider(Protocol):
    """Contrato del proveedor de pronósticos."""

    async def fetch(self, field: Field, *, start: date, days: int) -> list[ForecastPoint]:
        """Devuelve los pronósticos de `field` para `[start, start + days)`."""
        ...


def _truncate_to_hour(moment: datetime) -> datetime:
    """Normaliza a UTC y trunca a la hora: granularidad de una corrida del modelo."""
    return moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


class MockWeatherProvider:
    """Proveedor determinístico: misma semilla, mismo campo y día -> misma probabilidad."""

    def __init__(self, seed: int = 42, *, now: datetime | None = None) -> None:
        """`now` inyecta la corrida del modelo (tests); por defecto, el reloj real en UTC."""
        self.seed = seed
        self.now = now

    async def fetch(self, field: Field, *, start: date, days: int) -> list[ForecastPoint]:
        """Genera una probabilidad 0..100 por (día, `EventType`) usando `random.Random`.

        La semilla se compone con el id del campo, la fecha ISO y el valor del evento, así
        que el resultado no depende del orden de las consultas ni del resto del proceso.
        Todos los puntos de una corrida comparten `issued_at`: la hora de la corrida del
        modelo, truncada a la hora (docs/JOBS.md §Ingesta mock).
        """
        issued_at = _truncate_to_hour(self.now if self.now is not None else datetime.now(UTC))
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
                        issued_at=issued_at,
                    )
                )
        return points
