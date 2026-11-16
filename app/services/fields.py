"""Casos de uso de campos. TODO(S2).

Todas las funciones filtran por `user_id` (ownership). Un campo de otro usuario
se comporta como inexistente (NotFoundError, nunca 403) para no filtrar IDs.

async def create_field(session, user_id: int, data: FieldCreate) -> Field
    - (user_id, name) duplicado -> ConflictError("Field name already exists").
async def list_fields(session, user_id: int, *, limit: int, offset: int) -> Sequence[Field]
    - Orden: id ASC.
async def get_field(session, user_id: int, field_id: int) -> Field
    - No existe / no es del usuario -> NotFoundError("Field not found").
"""
