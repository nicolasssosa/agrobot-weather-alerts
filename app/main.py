"""Punto de entrada ASGI: `uvicorn app.main:app`."""

from fastapi import FastAPI

from app.api.routers import alerts, fields, forecasts, health, notifications, users
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging

API_PREFIX = "/api/v1"

_DESCRIPTION = """
API de alertas climáticas para Agrobot. Los usuarios registran sus campos, definen alertas
(campo + evento + umbral) y consultan los pronósticos y las notificaciones generadas.

## Autenticación

La autenticación es simulada: enviá el header `X-User-Id` con el id numérico del usuario.
En Swagger UI, usá el botón **Authorize** e ingresá el id. Sin header, con un id inválido o
con un usuario inexistente, la API responde **401** con
`{"detail": "Missing or invalid X-User-Id header"}`.

`POST /api/v1/users` y `GET /health` son públicos.

## Flujo típico

1. `POST /api/v1/users` — crear el usuario (devuelve su `id`).
2. `POST /api/v1/fields` — crear un campo del usuario autenticado.
3. `POST /api/v1/alerts` — definir una alerta para ese campo, evento y umbral. El `field_id`
   del ejemplo es un placeholder: usá el `id` del campo propio que devolvió el paso 2.
4. `GET /api/v1/fields/{field_id}/forecasts` — consultar los pronósticos del campo.
5. `GET /api/v1/notifications` — ver el historial de notificaciones.

Las notificaciones **no** las crea la API: las genera el worker (evaluador + dispatcher)
cuando el pronóstico de un campo supera el umbral de una alerta activa.
"""

_OPENAPI_TAGS = [
    {"name": "health", "description": "Estado del servicio y de la base de datos."},
    {"name": "users", "description": "Alta de usuarios y datos del usuario autenticado."},
    {"name": "fields", "description": "Campos georreferenciados del usuario."},
    {
        "name": "forecasts",
        "description": "Pronósticos por campo y evento (generados por la ingesta mock).",
    },
    {"name": "alerts", "description": "Reglas de alerta por campo, evento y umbral."},
    {
        "name": "notifications",
        "description": "Historial de notificaciones generadas por el worker.",
    },
]


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(
        title="Agrobot - Alertas Climáticas",
        version="0.1.0",
        description=_DESCRIPTION,
        openapi_tags=_OPENAPI_TAGS,
    )
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(users.router, prefix=API_PREFIX)
    app.include_router(fields.router, prefix=API_PREFIX)
    app.include_router(forecasts.router, prefix=API_PREFIX)
    app.include_router(alerts.router, prefix=API_PREFIX)
    app.include_router(notifications.router, prefix=API_PREFIX)
    return app


app = create_app()
