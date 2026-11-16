"""Router `users`.

TODO(S2): implementar los endpoints de docs/API.md §users.
Los handlers son finos: validan input (schemas), llaman al service
correspondiente en `app/services` y devuelven el schema de respuesta.
Sin lógica de negocio ni SQL acá.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/users", tags=["users"])
