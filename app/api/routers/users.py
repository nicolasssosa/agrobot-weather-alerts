"""Router `users`. Ver docs/API.md §users."""

from fastapi import APIRouter, status

from app.api.deps import CurrentUser, SessionDep
from app.models import User
from app.schemas.common import error_responses, protected_responses
from app.schemas.user import UserCreate, UserRead
from app.services.users import create_user

router = APIRouter(prefix="/users", tags=["users"])


@router.post(
    "",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    summary="Crear un usuario",
    responses=error_responses((409, "User with this phone number already exists")),
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
