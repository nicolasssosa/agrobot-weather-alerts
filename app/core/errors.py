"""Errores de dominio y su mapeo a HTTP.

Los services lanzan estas excepciones; NUNCA lanzan HTTPException.
`register_exception_handlers` (llamado en `create_app`) las traduce a:
    NotFoundError  -> 404 {"detail": "<mensaje>"}
    ConflictError  -> 409 {"detail": "<mensaje>"}
"""

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse


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


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DomainError, _domain_error_handler)
