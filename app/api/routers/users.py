"""Router `users`. Ver docs/API.md §users."""

import json
from typing import Any

from fastapi import APIRouter, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    IDEMPOTENT_REPLAYED_HEADER,
    CurrentUser,
    IdempotencyKeyDep,
    SessionDep,
    idempotent_response,
    render_json_body,
)
from app.models import User
from app.schemas.common import error_responses, protected_responses
from app.schemas.user import UserCreate, UserRead
from app.services.idempotency import compute_request_hash, run_idempotent
from app.services.users import create_user, get_user

router = APIRouter(prefix="/users", tags=["users"])

#: Alcance de la clave de idempotencia: método + ruta + body (docs/API.md §Idempotency-Key).
IDEMPOTENCY_SCOPE = "POST /api/v1/users"


def _user_body(user: User) -> dict[str, Any]:
    """Body de `UserRead` que se serializa en la respuesta."""
    return UserRead.model_validate(user).model_dump(mode="json")


def _scope_for(request_hash: str) -> str:
    """Acota la clave al body: la misma clave con otro body es una clave nueva.

    `POST /users` no tiene `user_id`, así que sin esto un cliente podría bloquear el alta
    de otro durante la vigencia de la clave (SEC-1). Entra en el `VARCHAR(64)` del scope.
    """
    return f"{IDEMPOTENCY_SCOPE}:{request_hash[:16]}"


async def _replay_user(session: AsyncSession, stored_body: str) -> str | None:
    """Reconstruye la respuesta desde el `{"id": …}` guardado (sin PII).

    Devuelve `None` si el usuario ya no existe: la clave se trata como nueva.
    """
    user_id = json.loads(stored_body)["id"]
    user = await get_user(session, int(user_id))
    if user is None:
        return None
    return render_json_body(_user_body(user))


@router.post(
    "",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    summary="Crear un usuario",
    responses={
        **error_responses((409, "User with this phone number already exists")),
        201: {"headers": {"Idempotent-Replayed": IDEMPOTENT_REPLAYED_HEADER}},
    },
)
async def create_user_endpoint(
    payload: UserCreate,
    session: SessionDep,
    idempotency_key: IdempotencyKeyDep,
) -> User | Response:
    """Crea un usuario (sin autenticación)."""
    if idempotency_key is None:
        return await create_user(session, payload)
    result = await run_idempotent(
        session,
        user_id=None,
        scope=_scope_for(compute_request_hash(payload)),
        key=idempotency_key,
        request_model=payload,
        operation=lambda: create_user(session, payload, commit=False),
        serializer=lambda user: render_json_body(_user_body(user)),
        stored_serializer=lambda user: render_json_body({"id": user.id}),
        replay_loader=lambda stored: _replay_user(session, stored),
        status_code=status.HTTP_201_CREATED,
    )
    return idempotent_response(result)


@router.get(
    "/me",
    response_model=UserRead,
    summary="Obtener el usuario autenticado",
    responses=protected_responses(),
)
async def read_current_user(user: CurrentUser) -> User:
    """Devuelve el usuario autenticado."""
    return user
