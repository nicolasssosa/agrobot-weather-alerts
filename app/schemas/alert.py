"""Schemas Pydantic v2 de `alert`. Ver docs/API.md §alerts."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from app.models import EventType


class AlertCreate(BaseModel):
    """Request de `POST /api/v1/alerts`.

    `field_id` no se acota acá: el service valida el rango BIGINT y el ownership, y ambos
    casos responden 404 "Field not found" (igual que un campo inexistente).
    """

    model_config = ConfigDict(extra="forbid")

    field_id: int
    event_type: EventType
    # `StrictInt`: rechaza bool, floats (70.0) y strings ("70"); la DB guarda SMALLINT.
    threshold: StrictInt = Field(ge=1, le=100)


class AlertUpdate(BaseModel):
    """Request de `PATCH /api/v1/alerts/{alert_id}`.

    `field_id` y `event_type` no son editables: crearlos como extra da 422. `{}` también es
    422 porque no hay nada que actualizar, y un `null` explícito no es un valor válido.
    """

    model_config = ConfigDict(extra="forbid")

    threshold: StrictInt | None = Field(default=None, ge=1, le=100)
    is_active: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def _require_a_non_null_field(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if not data:
                raise ValueError("at least one field must be provided")
            if any(value is None for value in data.values()):
                raise ValueError("fields must not be null")
        return data


class AlertRead(BaseModel):
    """Respuesta con los datos de una alerta."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    field_id: int
    event_type: EventType
    threshold: int
    is_active: bool
    created_at: datetime
    updated_at: datetime
