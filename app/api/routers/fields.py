"""Router `fields`. Ver docs/API.md §fields."""

from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.deps import CurrentUser, SessionDep
from app.models import Field
from app.schemas.common import PaginationParams, protected_responses
from app.schemas.field import FieldCreate, FieldPage, FieldRead
from app.services.fields import create_field, get_field, list_fields

router = APIRouter(prefix="/fields", tags=["fields"])


@router.post(
    "",
    response_model=FieldRead,
    status_code=status.HTTP_201_CREATED,
    summary="Crear un campo",
    responses=protected_responses((409, "Field name already exists")),
)
async def create_field_endpoint(
    payload: FieldCreate, user: CurrentUser, session: SessionDep
) -> Field:
    """Crea un campo del usuario autenticado."""
    return await create_field(session, user.id, payload)


@router.get(
    "",
    response_model=FieldPage,
    summary="Listar los campos del usuario",
    responses=protected_responses(),
)
async def list_fields_endpoint(
    user: CurrentUser,
    session: SessionDep,
    pagination: Annotated[PaginationParams, Depends()],
) -> FieldPage:
    """Lista paginada de los campos del usuario autenticado."""
    fields = await list_fields(session, user.id, limit=pagination.limit, offset=pagination.offset)
    return FieldPage(
        items=[FieldRead.model_validate(field) for field in fields],
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get(
    "/{field_id}",
    response_model=FieldRead,
    summary="Obtener un campo",
    responses=protected_responses((404, "Field not found")),
)
async def read_field_endpoint(field_id: int, user: CurrentUser, session: SessionDep) -> Field:
    """Devuelve un campo del usuario autenticado; 404 si no existe o es de otro usuario."""
    return await get_field(session, user.id, field_id)
