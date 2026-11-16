"""Schemas compartidos por la API. Ver docs/API.md §Convenciones."""

from typing import Annotated

from fastapi import Query
from pydantic import BaseModel

#: Mayor valor admitido por una columna `BIGINT` de Postgres (`id` y `offset`).
BIGINT_MAX = 2**63 - 1


class Page[T](BaseModel):
    """Respuesta paginada: `{"items": [...], "limit": 50, "offset": 0}`."""

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

    status: str
