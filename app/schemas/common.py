"""Schemas compartidos por la API. Ver docs/API.md §Convenciones."""

from typing import Annotated, Any, Final, Literal

from fastapi import Query
from pydantic import BaseModel, ConfigDict, Field

#: Mensaje único de la autenticación simulada (docs/API.md §Convenciones).
AUTH_ERROR_DETAIL: Final = "Missing or invalid X-User-Id header"

#: Mayor valor admitido por una columna `BIGINT` de Postgres (`id` y `offset`).
BIGINT_MAX = 2**63 - 1


class ErrorResponse(BaseModel):
    """Cuerpo de error de la API: `{"detail": "<mensaje>"}`."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"detail": AUTH_ERROR_DETAIL}]},
    )

    detail: str = Field(examples=[AUTH_ERROR_DETAIL])


def error_response(detail: str) -> dict[str, Any]:
    """Documenta una respuesta de error con `detail` como descripción y ejemplo."""
    return {
        "model": ErrorResponse,
        "description": detail,
        "content": {"application/json": {"example": {"detail": detail}}},
    }


def error_responses(*errors: tuple[int, str]) -> dict[int | str, dict[str, Any]]:
    """Construye `responses=` a partir de pares `(status_code, detail)`."""
    return {code: error_response(detail) for code, detail in errors}


def protected_responses(*extra: tuple[int, str]) -> dict[int | str, dict[str, Any]]:
    """Respuestas de un endpoint con `X-User-Id`: 401 más los errores propios."""
    return error_responses((401, AUTH_ERROR_DETAIL), *extra)


class Page[T](BaseModel):
    """Respuesta paginada: `{"items": [...], "limit": 50, "offset": 0}`."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"items": [], "limit": 50, "offset": 0}]},
    )

    items: list[T]
    limit: int
    offset: int


class PaginationParams:
    """Dependency de paginación: `limit` 1..100 (default 50) y `offset` >= 0."""

    def __init__(
        self,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0, le=BIGINT_MAX)] = 0,
    ) -> None:
        self.limit = limit
        self.offset = offset


class HealthStatus(BaseModel):
    """Resultado de `GET /health`."""

    model_config = ConfigDict(json_schema_extra={"examples": [{"status": "ok"}]})

    status: str


class UnavailableStatus(BaseModel):
    """Resultado de `GET /health` cuando la base de datos no responde."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"status": "unavailable"}]},
    )

    status: Literal["unavailable"]
