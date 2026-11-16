"""Schemas Pydantic v2 de `forecast`. Ver docs/API.md §forecasts."""

from datetime import date, datetime
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from app.models import EventType

#: 422 de `GET /api/v1/fields/{field_id}/forecasts`. El endpoint devuelve dos formas:
#: `HTTPValidationError` (`detail` lista) cuando falla la validación de un query param y
#: `ErrorResponse` (`detail` string) cuando `date_from > date_to` (docs/API.md §forecasts).
FORECASTS_422_RESPONSE: Final[dict[str, Any]] = {
    "description": (
        "Dos formas según el caso: un query param inválido (`date_from`/`date_to` mal "
        "formados o `event_type` desconocido) responde `HTTPValidationError` con `detail` "
        "como lista de errores; `date_from` posterior a `date_to` responde `ErrorResponse` "
        "con `detail` string."
    ),
    "content": {
        "application/json": {
            "schema": {
                "anyOf": [
                    {"$ref": "#/components/schemas/HTTPValidationError"},
                    {"$ref": "#/components/schemas/ErrorResponse"},
                ]
            },
            "examples": {
                "invalid_query_param": {
                    "summary": "Query param inválido (validación automática de FastAPI)",
                    "value": {
                        "detail": [
                            {
                                "type": "enum",
                                "loc": ["query", "event_type"],
                                "msg": (
                                    "Input should be 'frost', 'rain', 'hail', "
                                    "'strong_wind' or 'heat_wave'"
                                ),
                                "input": "bogus",
                            }
                        ]
                    },
                },
                "date_range": {
                    "summary": "Rango de fechas invertido (validación manual del router)",
                    "value": {"detail": "date_from must be <= date_to"},
                },
            },
        }
    },
}


class ForecastRead(BaseModel):
    """Un pronóstico expuesto por la API (sin ids ni timestamps internos)."""

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    "forecast_date": "2026-11-17",
                    "event_type": "frost",
                    "probability": 72,
                    "source": "mock",
                    "fetched_at": "2026-11-16T12:00:00Z",
                }
            ],
        },
    )

    forecast_date: date
    event_type: EventType
    probability: int
    source: str
    fetched_at: datetime
