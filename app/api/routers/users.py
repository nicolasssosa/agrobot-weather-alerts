"""Router `users`. Ver docs/API.md §users."""

from fastapi import APIRouter, status

from app.api.deps import CurrentUser, SessionDep
from app.models import User
from app.schemas.common import error_responses, protected_responses
from app.schemas.user import USERS_422_RESPONSE, UserCreate, UserRead, UserUpdate
from app.services.users import create_user, update_user

router = APIRouter(prefix="/users", tags=["users"])


@router.post(
    "",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    summary="Crear un usuario",
    responses={
        **error_responses((409, "User with this phone number already exists")),
        422: USERS_422_RESPONSE,
    },
)
async def create_user_endpoint(payload: UserCreate, session: SessionDep) -> User:
    """Crea un usuario (sin autenticación)."""
    return await create_user(session, payload)


@router.get(
    "/me",
    response_model=UserRead,
    summary="Obtener el usuario autenticado",
    responses=protected_responses(),
)
async def read_current_user(user: CurrentUser) -> User:
    """Devuelve el usuario autenticado."""
    return user


@router.patch(
    "/me",
    response_model=UserRead,
    summary="Actualizar el usuario autenticado",
    responses={**protected_responses(), 422: USERS_422_RESPONSE},
)
async def update_current_user_endpoint(
    payload: UserUpdate,
    user: CurrentUser,
    session: SessionDep,
) -> User:
    """Actualiza la zona horaria y/o el horario de silencio del usuario autenticado."""
    return await update_user(session, user, payload)
