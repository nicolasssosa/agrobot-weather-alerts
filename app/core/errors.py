"""Errores de dominio y su mapeo a HTTP.

Los services lanzan estas excepciones; NUNCA lanzan HTTPException.
`register_exception_handlers` (llamado en `create_app`) las traduce a:
    NotFoundError  -> 404 {"detail": "<mensaje>"}
    ConflictError  -> 409 {"detail": "<mensaje>"}
    RequestValidationError -> 422 {"detail": [<error de FastAPI saneado>]}
    DBAPIError con sqlstate 55P03/57014 -> 503 {"detail": "Database busy, try again later"}
"""

import logging
import math
from collections.abc import Sequence
from itertools import islice
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError

logger = logging.getLogger(__name__)


class DomainError(Exception):
    status_code: int = status.HTTP_400_BAD_REQUEST

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class NotFoundError(DomainError):
    status_code = status.HTTP_404_NOT_FOUND


class ConflictError(DomainError):
    status_code = status.HTTP_409_CONFLICT


async def _domain_error_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, DomainError)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


# Cotas del saneo: el `input` de un error de validación lo controla el cliente, así que
# sin límites un body anidado (RecursionError -> 500) o gigante (respuesta de MBs) lo rompe.
_MAX_JSON_DEPTH = 32
_MAX_STRING_LENGTH = 200
_MAX_CONTAINER_ITEMS = 20
_TRUNCATED = "<truncated>"


def _sanitize_string(value: str) -> str:
    """Escapa surrogates sueltos y trunca strings largos a `_MAX_STRING_LENGTH`."""
    escaped = value.encode("utf-8", "backslashreplace").decode("utf-8")
    if len(escaped) > _MAX_STRING_LENGTH:
        return escaped[:_MAX_STRING_LENGTH] + "…"
    return escaped


def _sanitize_json_value(value: object, *, depth: int = 0) -> object:
    """Convierte un valor de un error de validación en algo JSON-serializable.

    Un surrogate UTF-16 suelto (`"a\\ud800b"`) no se puede codificar en UTF-8 y rompería el
    422 (500). Se escapa con `backslashreplace`; los floats no finitos (NaN/Infinity) se
    etiquetan como string; los objetos arbitrarios de `ctx` se convierten a texto.

    El valor viene del cliente: la recursión tiene cota (`_MAX_JSON_DEPTH`), los strings se
    truncan y las listas/dicts conservan solo los primeros `_MAX_CONTAINER_ITEMS` elementos.
    """
    if depth >= _MAX_JSON_DEPTH:
        return _TRUNCATED
    if isinstance(value, str):
        return _sanitize_string(value)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, dict):
        sanitized = {
            str(_sanitize_json_value(key, depth=depth + 1)): _sanitize_json_value(
                item, depth=depth + 1
            )
            for key, item in islice(value.items(), _MAX_CONTAINER_ITEMS)
        }
        if len(value) > _MAX_CONTAINER_ITEMS:
            sanitized[_TRUNCATED] = _TRUNCATED
        return sanitized
    if isinstance(value, (list, tuple)):
        sanitized_items: list[object] = [
            _sanitize_json_value(item, depth=depth + 1) for item in value[:_MAX_CONTAINER_ITEMS]
        ]
        if len(value) > _MAX_CONTAINER_ITEMS:
            sanitized_items.append(_TRUNCATED)
        return sanitized_items
    return str(value)


def _sanitize_validation_errors(errors: Sequence[Any]) -> list[dict[str, object]]:
    """Conserva `loc`, `msg`, `type`, etc. de FastAPI, saneando `input` y `ctx`."""
    sanitized: list[dict[str, object]] = []
    for error in errors:
        if isinstance(error, dict):
            sanitized.append(
                {str(key): _sanitize_json_value(value) for key, value in error.items()}
            )
        else:
            sanitized.append({"msg": _sanitize_json_value(error)})
    return sanitized


#: Tope de errores devueltos en `detail` (docs/API.md §Convenciones, S14a): un body con
#: miles de claves inválidas no debe producir una respuesta de MBs.
_MAX_VALIDATION_ERRORS = 20

#: Detalle del 503 por timeout de base de datos (docs/API.md §Convenciones, S14a).
DB_BUSY_DETAIL = "Database busy, try again later"

#: sqlstates de Postgres que significan "la base está ocupada, reintentá":
#: `55P03` = lock_not_available (`lock_timeout`), `57014` = query_canceled
#: (`statement_timeout` o cancelación de la consulta).
_DB_TIMEOUT_SQLSTATES = frozenset({"55P03", "57014"})


def _bounded_validation_errors(errors: Sequence[Any]) -> list[dict[str, object]]:
    """Sanea los errores y deja como máximo `_MAX_VALIDATION_ERRORS` + un resumen.

    Si había más, el último elemento es
    `{"loc": [], "msg": "<n> more errors omitted", "type": "too_many_errors"}` con `n` la
    cantidad omitida (docs/API.md §Convenciones). Con 20 o menos la respuesta no cambia.
    """
    sanitized = _sanitize_validation_errors(errors)
    if len(sanitized) <= _MAX_VALIDATION_ERRORS:
        return sanitized
    omitted = len(sanitized) - _MAX_VALIDATION_ERRORS
    bounded = sanitized[:_MAX_VALIDATION_ERRORS]
    bounded.append({"loc": [], "msg": f"{omitted} more errors omitted", "type": "too_many_errors"})
    return bounded


async def _request_validation_error_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    # 422 literal: `status.HTTP_422_UNPROCESSABLE_ENTITY` está deprecado en Starlette reciente.
    return JSONResponse(
        status_code=422,
        content={"detail": _bounded_validation_errors(exc.errors())},
    )


def _db_sqlstate(exc: DBAPIError) -> str | None:
    """sqlstate del error de Postgres, igual que `app/services/_db_errors.py` (S14a).

    asyncpg expone `sqlstate` en `orig` (o en su `__cause__`) y algunas versiones de
    psycopg/DBAPI usan `pgcode`: se aceptan las dos formas.
    """
    orig = exc.orig
    for candidate in (orig, getattr(orig, "__cause__", None)):
        sqlstate = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
        if sqlstate is not None:
            return str(sqlstate)
    return None


async def _db_timeout_handler(request: Request, exc: Exception) -> JSONResponse:
    """`lock_timeout` / `statement_timeout` -> 503 con `Retry-After` (docs/API.md, S14a).

    Cualquier otro error de base de datos no es un "reintentá más tarde": se re-lanza y
    sigue el comportamiento de siempre (500).
    """
    assert isinstance(exc, DBAPIError)
    sqlstate = _db_sqlstate(exc)
    if sqlstate not in _DB_TIMEOUT_SQLSTATES:
        raise exc
    logger.warning("db_timeout sqlstate=%s path=%s", sqlstate, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": DB_BUSY_DETAIL},
        headers={"Retry-After": "1"},
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DomainError, _domain_error_handler)
    app.add_exception_handler(RequestValidationError, _request_validation_error_handler)
    app.add_exception_handler(DBAPIError, _db_timeout_handler)
