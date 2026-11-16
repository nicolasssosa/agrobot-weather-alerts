"""Router `users`. Ver docs/API.md §users."""

from fastapi import APIRouter, status

from app.api.deps import CurrentUser, SessionDep
from app.models import User
from app.schemas.user import UserCreate, UserRead
from app.services.users import create_user

router = APIRouter(prefix="/users", tags=["users"])


@router.post("", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def create_user_endpoint(payload: UserCreate, session: SessionDep) -> User:
    """Crea un usuario (sin autenticación)."""
    return await create_user(session, payload)


@router.get("/me", response_model=UserRead)
async def read_current_user(user: CurrentUser) -> User:
    """Devuelve el usuario autenticado."""
    return user
