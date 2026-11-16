"""Schemas Pydantic v2 de `user`. Ver docs/API.md §users."""

import unicodedata
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Mismo patrón ASCII que el CHECK `phone_number_e164` de la DB (`[0-9]`, no `\d`).
PHONE_E164_PATTERN = r"^\+[1-9][0-9]{7,14}$"


class UserCreate(BaseModel):
    """Request de `POST /api/v1/users`."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [{"phone_number": "+5491100000001", "name": "Juan Pérez"}],
        },
    )

    phone_number: str = Field(pattern=PHONE_E164_PATTERN)
    name: str = Field(min_length=1, max_length=120)

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
                    "created_at": "2026-11-16T12:00:00Z",
                }
            ],
        },
    )

    id: int
    phone_number: str
    name: str
    created_at: datetime
