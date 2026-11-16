"""Proveedor meteorológico (mock del servicio externo). TODO(S3).

Spec: docs/JOBS.md §Ingesta mock.

@dataclass(frozen=True, slots=True)
class ForecastPoint:
    field_id: int
    forecast_date: date
    event_type: EventType
    probability: int  # 0..100

class WeatherProvider(Protocol):
    async def fetch(self, field: Field, *, start: date, days: int) -> list[ForecastPoint]

class MockWeatherProvider:
    def __init__(self, seed: int = 42) -> None
    async def fetch(...) -> list[ForecastPoint]
        - Determinístico: random.Random(f"{seed}:{field.id}:{day.isoformat()}:{event}").
        - Una entrada por (día, event_type) para TODOS los EventType.
"""
