"""Schemas Pydantic v2 de `field`. Ver docs/API.md §fields."""

import math
import unicodedata
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import Page


class FieldCreate(BaseModel):
    """Request de `POST /api/v1/fields`."""

    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        json_schema_extra={
            "examples": [{"name": "Lote Norte", "latitude": -34.6037, "longitude": -58.3816}],
        },
    )

    name: str = Field(min_length=1, max_length=120)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must not be blank")
        return stripped

    @field_validator("name")
    @classmethod
    def _reject_control_characters(cls, value: str) -> str:
        if any(unicodedata.category(char) == "Cc" for char in value):
            raise ValueError("name must not contain control characters")
        return value

    @field_validator("latitude", "longitude", mode="before")
    @classmethod
    def _label_non_finite(cls, value: object) -> object:
        """Etiqueta NaN/±Infinity como string: el 422 de FastAPI debe ser JSON válido.

        El valor se sigue rechazando (`allow_inf_nan=False`); sólo cambia cómo se
        serializa el `input` del error, que `json.dumps` no puede emitir como NaN.
        """
        if isinstance(value, float) and not math.isfinite(value):
            return str(value)
        return value


class FieldRead(BaseModel):
    """Respuesta con los datos de un campo."""

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    "id": 1,
                    "name": "Lote Norte",
                    "latitude": -34.6037,
                    "longitude": -58.3816,
                    "created_at": "2026-11-16T12:00:00Z",
                }
            ],
        },
    )

    id: int
    name: str
    latitude: float
    longitude: float
    created_at: datetime


class FieldPage(Page[FieldRead]):
    """Respuesta paginada de `GET /api/v1/fields`."""
