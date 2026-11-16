"""Dependencies compartidas de la API. Ver docs/API.md §Autenticación (simulada)."""

import re
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.models import User
from app.schemas.common import BIGINT_MAX
from app.services.users import get_user

SessionDep = Annotated[AsyncSession, Depends(get_session)]

_AUTH_ERROR_DETAIL = "Missing or invalid X-User-Id header"
# Solo dígitos ASCII: `int("+1")`, `int(" 1 ")` e `int("\u0661")` (dígito árabe) serían aceptados.
_ID_HEADER_PATTERN = re.compile(r"[0-9]+")
_MAX_ID_DIGITS = len(str(BIGINT_MAX))


def _invalid_credentials() -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_AUTH_ERROR_DETAIL)


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
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
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
