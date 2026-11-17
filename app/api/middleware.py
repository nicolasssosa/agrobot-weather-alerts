"""Middleware ASGI de tamaño máximo del body de un request (docs/API.md §Convenciones).

`MAX_REQUEST_BODY_BYTES` (S14a, D16) acota el body de **todos** los endpoints: un body más
grande que el límite responde 413 `{"detail": "Request body too large"}` sin que la app lo
procese. Es ASGI puro (no `BaseHTTPMiddleware`): así se corta antes de leer el body y sin
envolver la respuesta en una task extra.

Dos caminos:
- Con `Content-Length` declarado, se rechaza antes de leer nada.
- Sin `Content-Length` (chunked), se cuentan los bytes a medida que llegan y se corta al
  superar el límite con el mismo 413. Exactamente el límite pasa.

En el caso chunked el 413 se envía desde el wrapper de `receive` y la lectura se corta
devolviendo `http.disconnect`. No se puede "lanzar" el error: al cortar la lectura del body,
FastAPI falla al parsearlo y produce su propia respuesta. Como el 413 ya salió, esa respuesta
se descarta y el cliente ve el 413.
"""

import logging
from typing import Final

from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

#: Detalle exacto del 413 (docs/API.md §Convenciones).
BODY_TOO_LARGE_DETAIL: Final = "Request body too large"


def _declared_content_length(scope: Scope) -> int | None:
    """`Content-Length` del request, o `None` si falta o no es un entero válido."""
    for name, value in scope["headers"]:
        if name == b"content-length":
            try:
                length = int(value)
            except ValueError:
                return None
            return length if length >= 0 else None
    return None


async def _send_too_large(scope: Scope, receive: Receive, send: Send) -> None:
    """Responde el 413 del contrato (JSON, como el resto de los errores de la API)."""
    response = JSONResponse(status_code=413, content={"detail": BODY_TOO_LARGE_DETAIL})
    await response(scope, receive, send)


class MaxRequestBodySizeMiddleware:
    """Rechaza con 413 un body mayor a `max_body_bytes` (con o sin `Content-Length`)."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope["path"]
        declared = _declared_content_length(scope)
        if declared is not None and declared > self.max_body_bytes:
            logger.warning("request_body_too_large path=%s limit=%d", path, self.max_body_bytes)
            await _send_too_large(scope, receive, send)
            return

        received = 0
        exceeded = False
        response_started = False
        rejected = False

        async def reject() -> None:
            """Envía el 413 una sola vez, si la app todavía no empezó a responder."""
            nonlocal rejected
            if rejected or response_started:
                return
            rejected = True
            logger.warning("request_body_too_large path=%s limit=%d", path, self.max_body_bytes)
            await _send_too_large(scope, receive, send)

        async def limited_receive() -> Message:
            """Cuenta los bytes del body y corta la lectura al superar el límite."""
            nonlocal received, exceeded
            if exceeded:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    exceeded = True
                    await reject()
                    return {"type": "http.disconnect"}
            return message

        async def tracking_send(message: Message) -> None:
            """Descarta la respuesta de la app si el 413 ya salió."""
            nonlocal response_started
            if rejected:
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        await self.app(scope, limited_receive, tracking_send)
