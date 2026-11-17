"""Dependencies compartidas de la API. Ver docs/API.md §Autenticación (simulada)."""

import re
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Security, status
from fastapi.responses import JSONResponse, Response
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.models import User
from app.schemas.common import AUTH_ERROR_DETAIL, BIGINT_MAX
from app.services.idempotency import INVALID_KEY_DETAIL, IdempotentResult
from app.services.users import get_user

SessionDep = Annotated[AsyncSession, Depends(get_session)]

# `auto_error=False`: el 401 lo arma `get_current_user` con el mensaje único de la API.
_x_user_id_header = APIKeyHeader(
    name="X-User-Id",
    scheme_name="X-User-Id",
    auto_error=False,
    description=(
        "Id numérico del usuario autenticado (autenticación simulada). "
        "Usá el botón **Authorize** de Swagger UI."
    ),
)
# Solo dígitos ASCII: `int("+1")`, `int(" 1 ")` e `int("\u0661")` (dígito árabe) serían aceptados.
_ID_HEADER_PATTERN = re.compile(r"[0-9]+")
_MAX_ID_DIGITS = len(str(BIGINT_MAX))


def _invalid_credentials() -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=AUTH_ERROR_DETAIL)


def _parse_user_id(raw: str) -> int | None:
    """Devuelve el id si es un entero positivo ASCII dentro del rango BIGINT; si no, `None`."""
    if _ID_HEADER_PATTERN.fullmatch(raw) is None or len(raw) > _MAX_ID_DIGITS:
        return None
    user_id = int(raw)
    if not 1 <= user_id <= BIGINT_MAX:
        return None
    return user_id


async def get_current_user(
    session: SessionDep,
    x_user_id: Annotated[str | None, Security(_x_user_id_header)] = None,
) -> User:
    """Resuelve el usuario autenticado a partir del header `X-User-Id`."""
    if x_user_id is None:
        raise _invalid_credentials()
    user_id = _parse_user_id(x_user_id)
    if user_id is None:
        raise _invalid_credentials()
    user = await get_user(session, user_id)
    if user is None:
        raise _invalid_credentials()
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]

# --- Idempotency-Key (docs/API.md §Idempotency-Key) --------------------------

#: Header opcional: 1..255 caracteres ASCII imprimibles, sin espacios (ni controles).
_IDEMPOTENCY_KEY_PATTERN = re.compile(r"[\x21-\x7E]{1,255}")

#: Header de respuesta que marca un replay (solo en las respuestas repetidas).
IDEMPOTENT_REPLAYED_HEADER: dict[str, Any] = {
    "description": (
        "`true` solo cuando el body es el replay de una respuesta guardada con el mismo "
        "`Idempotency-Key` (no se volvió a ejecutar la operación)."
    ),
    "schema": {"type": "string", "enum": ["true"]},
}


def validate_idempotency_key(value: str) -> str:
    """Valida el header; si no cumple, 422 con el `detail` exacto de la API."""
    if _IDEMPOTENCY_KEY_PATTERN.fullmatch(value) is None:
        # 422 literal: `status.HTTP_422_UNPROCESSABLE_ENTITY` está deprecado en Starlette.
        raise HTTPException(status_code=422, detail=INVALID_KEY_DETAIL)
    return value


async def get_idempotency_key(
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            description=(
                "Header `Idempotency-Key` opcional para reintentos seguros "
                "(1-255 caracteres ASCII imprimibles, sin espacios). La misma clave con el "
                "mismo body devuelve la respuesta original (header `Idempotent-Replayed: true`); "
                "con otro body, 422. Vigencia: `IDEMPOTENCY_TTL_HOURS` (default 24 h). La clave "
                "es por usuario y por endpoint."
            ),
            examples=["7f5c2b1e-4a9d-4c3e-9f2a-1b8e6d0c4a51"],
        ),
    ] = None,
) -> str | None:
    """Resuelve y valida el header opcional `Idempotency-Key`."""
    if idempotency_key is None:
        return None
    return validate_idempotency_key(idempotency_key)


IdempotencyKeyDep = Annotated[str | None, Depends(get_idempotency_key)]


def render_json_body(body: dict[str, Any]) -> str:
    """Serializa el body con el mismo encoder que `JSONResponse` (replay byte a byte)."""
    return bytes(JSONResponse(content=body).body).decode("utf-8")


def idempotent_response(result: IdempotentResult) -> Response:
    """Arma la respuesta con el status y el body guardados (o los recién generados)."""
    headers = {"Idempotent-Replayed": "true"} if result.replayed else None
    return Response(
        content=result.body,
        status_code=result.status_code,
        media_type="application/json",
        headers=headers,
    )
