"""Schemas Pydantic v2 de `user`. Ver docs/API.md §users."""

import unicodedata
from datetime import datetime, time
from typing import Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from app.models.field import DEFAULT_TIMEZONE
from app.schemas.field import NON_IANA_TIMEZONES

# Mismo patrón ASCII que el CHECK `phone_number_e164` de la DB (`[0-9]`, no `\d`).
PHONE_E164_PATTERN = r"^\+[1-9][0-9]{7,14}$"

#: Mismo mensaje que `POST /fields` para una zona con formato inválido (API.md §users).
_TZ_ERROR = (
    "invalid IANA timezone: {value!r}; expected a name from the IANA tz database "
    f"(e.g. {DEFAULT_TIMEZONE!r})"
)

#: 422 del `PATCH /users/me`: `HTTPValidationError` si el schema rechaza el body y
#: `ErrorResponse` si la zona tiene formato válido pero PostgreSQL no la reconoce.
USERS_422_RESPONSE: Final[dict[str, Any]] = {
    "description": (
        "Dos formas según el caso: un body inválido (o una zona con formato inválido) responde "
        "`HTTPValidationError` con `detail` como lista de errores; una zona que PostgreSQL no "
        "reconoce con coincidencia exacta (`pg_timezone_names`) responde `ErrorResponse` con "
        "`detail` string."
    ),
    "content": {
        "application/json": {
            "schema": {
                "anyOf": [
                    {"$ref": "#/components/schemas/HTTPValidationError"},
                    {"$ref": "#/components/schemas/ErrorResponse"},
                ]
            },
        }
    },
}


def _validate_iana_timezone(value: str) -> str:
    """Misma validación de formato que `fields.timezone` (docs/API.md §users, S13).

    La validez **autoritativa** la da PostgreSQL (`pg_timezone_names`, ver
    `app.services.fields`): acá solo se descartan las formas imposibles (vacío, rutas, no
    ASCII, zonas que no identifican una región) y las claves ausentes de la tz database.
    """
    candidate = value.strip()
    if (
        not candidate
        or not candidate.isascii()
        or candidate.startswith("/")
        or ".." in candidate
        or "\\" in candidate
        or candidate in NON_IANA_TIMEZONES
    ):
        raise ValueError(_TZ_ERROR.format(value=value))
    try:
        ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(_TZ_ERROR.format(value=value)) from None
    return candidate


class QuietHours(BaseModel):
    """Ventana de silencio en hora local: `[start, end)`; `start > end` cruza la medianoche.

    Solo horas `HH:MM` (00:00-23:59): segundos distintos de cero, un offset de zona en el
    valor o `start == end` son 422 (docs/API.md §users).
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"start": "22:00", "end": "07:00"}]},
    )

    start: time
    end: time

    @model_validator(mode="after")
    def _validate_window(self) -> "QuietHours":
        for boundary in (self.start, self.end):
            if boundary.tzinfo is not None:
                raise ValueError("quiet hours must be local times without a timezone offset")
            if boundary.second or boundary.microsecond:
                raise ValueError("quiet hours must use HH:MM (seconds must be zero)")
        if self.start == self.end:
            raise ValueError("quiet_hours start and end must differ")
        return self

    @field_serializer("start", "end")
    def _serialize_time(self, value: time) -> str:
        """Serializa `HH:MM` (Pydantic usaría `HH:MM:SS` por defecto)."""
        return value.strftime("%H:%M")


class UserCreate(BaseModel):
    """Request de `POST /api/v1/users`."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "phone_number": "+5491100000001",
                    "name": "Juan Pérez",
                    "timezone": DEFAULT_TIMEZONE,
                    "quiet_hours": {"start": "22:00", "end": "07:00"},
                }
            ],
        },
    )

    phone_number: str = Field(pattern=PHONE_E164_PATTERN)
    name: str = Field(min_length=1, max_length=120)
    #: Zona IANA del usuario (S13); define su horario de silencio.
    timezone: str = Field(default=DEFAULT_TIMEZONE, max_length=64)
    #: Silencio opcional; `None` = sin silencio (S13).
    quiet_hours: QuietHours | None = None

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

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        return _validate_iana_timezone(value)


class UserUpdate(BaseModel):
    """Request de `PATCH /api/v1/users/me`.

    Todos los campos son opcionales pero el body vacío es 422. Un `null` **explícito** en
    `quiet_hours` desactiva el silencio; omitir la clave no lo cambia (se distingue con
    `model_fields_set` en el service). `timezone` no admite `null` (la columna es NOT NULL).
    `phone_number` y `name` no son editables: crearlos como extra da 422.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "timezone": "America/Argentina/Cordoba",
                    "quiet_hours": {"start": "22:00", "end": "07:00"},
                }
            ],
        },
    )

    timezone: str | None = Field(default=None, max_length=64)
    quiet_hours: QuietHours | None = None

    @model_validator(mode="before")
    @classmethod
    def _require_a_non_null_timezone(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if not data:
                raise ValueError("at least one field must be provided")
            if "timezone" in data and data["timezone"] is None:
                raise ValueError("timezone must not be null")
        return data

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str | None) -> str | None:
        return None if value is None else _validate_iana_timezone(value)


class UserRead(BaseModel):
    """Respuesta con los datos públicos de un usuario."""

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    "id": 1,
                    "phone_number": "+5491100000001",
                    "name": "Juan Pérez",
                    "timezone": DEFAULT_TIMEZONE,
                    "quiet_hours": {"start": "22:00", "end": "07:00"},
                    "created_at": "2026-11-16T12:00:00Z",
                }
            ],
        },
    )

    id: int
    phone_number: str
    name: str
    timezone: str
    #: `{"start": "HH:MM", "end": "HH:MM"}` o `null` si no configuró silencio (S13).
    quiet_hours: QuietHours | None
    created_at: datetime

    @model_validator(mode="before")
    @classmethod
    def _build_quiet_hours(cls, data: Any) -> Any:
        """Junta `quiet_hours_start/end` del ORM en el objeto `quiet_hours` del contrato."""
        if isinstance(data, dict):
            return data
        start = getattr(data, "quiet_hours_start", None)
        end = getattr(data, "quiet_hours_end", None)
        return {
            "id": data.id,
            "phone_number": data.phone_number,
            "name": data.name,
            "timezone": data.timezone,
            "quiet_hours": (
                {"start": start, "end": end} if start is not None and end is not None else None
            ),
            "created_at": data.created_at,
        }
