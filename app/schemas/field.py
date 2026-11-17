"""Schemas Pydantic v2 de `field`. Ver docs/API.md §fields."""

import math
import unicodedata
from datetime import datetime
from typing import Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.field import DEFAULT_TIMEZONE
from app.schemas.common import Page

#: Zonas de la tz database que no identifican una región: `Factory` es un placeholder de
#: tzdata y `posixrules`/`localtime` son archivos del sistema, no nombres IANA (docs/API.md
#: §fields). Postgres las resuelve, pero no sirven para calcular el "hoy" de un campo.
NON_IANA_TIMEZONES: Final[frozenset[str]] = frozenset({"localtime", "posixrules", "Factory"})

_TZ_ERROR = (
    "invalid IANA timezone: {value!r}; expected a name from the IANA tz database "
    f"(e.g. {DEFAULT_TIMEZONE!r})"
)

#: 422 de `POST /api/v1/fields`. El endpoint devuelve dos formas: `HTTPValidationError`
#: (`detail` lista) si el schema rechaza el body y `ErrorResponse` (`detail` string) si la zona
#: existe en el formato pero Postgres no la reconoce (`pg_timezone_names`, S12a CR-1).
FIELDS_422_RESPONSE: Final[dict[str, Any]] = {
    "description": (
        "Dos formas según el caso: un body inválido o una zona con formato inválido responde "
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
            "examples": {
                "invalid_body": {
                    "summary": "Body inválido (validación automática de FastAPI)",
                    "value": {
                        "detail": [
                            {
                                "type": "less_than_equal",
                                "loc": ["body", "latitude"],
                                "msg": "Input should be less than or equal to 90",
                                "input": 91,
                            }
                        ]
                    },
                },
                "unknown_timezone": {
                    "summary": "Zona desconocida para PostgreSQL (validación del service)",
                    "value": {
                        "detail": (
                            "Invalid timezone 'localtime': it is not a timezone recognized "
                            "by PostgreSQL (pg_timezone_names)"
                        )
                    },
                },
            },
        }
    },
}


class FieldCreate(BaseModel):
    """Request de `POST /api/v1/fields`."""

    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        json_schema_extra={
            "examples": [
                {
                    "name": "Lote Norte",
                    "latitude": -34.6037,
                    "longitude": -58.3816,
                    "timezone": DEFAULT_TIMEZONE,
                }
            ],
        },
    )

    name: str = Field(min_length=1, max_length=120)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    #: Zona IANA del campo (D9); define su "hoy" local al evaluar alertas.
    timezone: str = Field(default=DEFAULT_TIMEZONE, max_length=64)

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

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        """Primera barrera barata (S12a): formato, ASCII y tz database de `zoneinfo`.

        La validez **autoritativa** la da PostgreSQL (`pg_timezone_names`, ver
        `app.services.fields`): `zoneinfo` puede aceptar nombres que su tz database local
        resuelve y Postgres no (p. ej. variantes de mayúsculas en un filesystem
        case-insensitive), y un solo campo así rompería el evaluador para todos.

        Se rechazan strings vacíos, rutas (`/...`, `../...`), nombres no ASCII (toda clave
        IANA es ASCII), las zonas que no identifican una región (`NON_IANA_TIMEZONES`) y
        cualquier clave que no exista en la tz database. `ZoneInfo` acepta claves válidas
        como `UTC` o `Etc/GMT+3`, que sí son nombres IANA.
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
                    "timezone": DEFAULT_TIMEZONE,
                    "created_at": "2026-11-16T12:00:00Z",
                }
            ],
        },
    )

    id: int
    name: str
    latitude: float
    longitude: float
    timezone: str
    created_at: datetime


class FieldPage(Page[FieldRead]):
    """Respuesta paginada de `GET /api/v1/fields`."""
