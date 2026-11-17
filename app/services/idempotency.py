"""Idempotencia de `POST /alerts` con header `Idempotency-Key`. Ver docs/API.md §Idempotency-Key.

Un único punto de orquestación (`run_idempotent`):

1. `pg_advisory_xact_lock` sobre `(user_id, scope, key)` serializa los requests concurrentes
   con la misma clave (el lock se libera al terminar la transacción).
2. Si hay una clave vigente con el mismo `request_hash`, se devuelve la respuesta guardada
   (replay, byte a byte); si el hash difiere -> 422; si está vencida, se borra y se trata
   como nueva.
3. La operación de negocio se ejecuta sin commitear (`commit=False`) y la fila de
   `idempotency_keys` se inserta en la MISMA transacción: o quedan las dos, o ninguna.
   Cualquier error (de dominio o de la base) revierte todo y se propaga.

La operación y la serialización de la respuesta las define el router; acá no se conoce
ningún schema concreto. `response_body` es TEXT con los bytes exactos de la respuesta
(docs/DATA_MODEL.md §idempotency_keys).
"""

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta

from pydantic import BaseModel
from sqlalchemy import ColumnElement, delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import DomainError
from app.models import IdempotencyKey

logger = logging.getLogger(__name__)

#: Mensaje exacto del 422 cuando falta el header (docs/API.md §Idempotency-Key).
MISSING_KEY_DETAIL = "Idempotency-Key header is required"

#: Mensaje exacto del 422 con un header mal formado (docs/API.md §Idempotency-Key).
INVALID_KEY_DETAIL = "Invalid Idempotency-Key"

#: Mensaje exacto del 422 cuando la clave se reusa con otro body (docs/API.md).
KEY_REUSED_DETAIL = "Idempotency-Key reused with a different request"


class IdempotencyKeyReusedError(DomainError):
    """Misma clave con un body distinto: 422 con un `detail` plano (no el 422 de FastAPI)."""

    status_code = 422


@dataclass(frozen=True, slots=True)
class IdempotentResult:
    """Respuesta a devolver: la recién ejecutada o el replay de la guardada.

    `body` es el texto ya serializado, byte a byte igual al de la respuesta original.
    """

    status_code: int
    body: str
    replayed: bool


def compute_request_hash(request_model: BaseModel) -> str:
    """SHA-256 del request ya validado y normalizado (el orden de claves no importa)."""
    payload = json.dumps(
        request_model.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _key_filters(user_id: int, scope: str, key: str) -> tuple[ColumnElement[bool], ...]:
    """Filtro de la clave: por usuario y por endpoint."""
    return (
        IdempotencyKey.user_id == user_id,
        IdempotencyKey.scope == scope,
        IdempotencyKey.key == key,
    )


async def _lock(session: AsyncSession, *, user_id: int, scope: str, key: str) -> None:
    """Toma el advisory lock de transacción de la clave (serializa los concurrentes)."""
    lock_key = f"{user_id}|{scope}|{key}"
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0))))


async def _store(
    session: AsyncSession,
    *,
    user_id: int,
    scope: str,
    key: str,
    request_hash: str,
    status_code: int,
    body: str,
) -> None:
    """Inserta la respuesta guardada en la transacción abierta (sin commitear).

    `expires_at` se calcula con el reloj de la base (`created_at + IDEMPOTENCY_TTL_HOURS`).
    """
    await session.execute(
        insert(IdempotencyKey).values(
            user_id=user_id,
            scope=scope,
            key=key,
            request_hash=request_hash,
            status_code=status_code,
            response_body=body,
            expires_at=func.now() + timedelta(hours=get_settings().idempotency_ttl_hours),
        )
    )


async def run_idempotent[T](
    session: AsyncSession,
    *,
    user_id: int,
    scope: str,
    key: str,
    request_model: BaseModel,
    operation: Callable[[], Awaitable[T]],
    serializer: Callable[[T], str],
    status_code: int,
) -> IdempotentResult:
    """Ejecuta `operation` una sola vez por clave y guarda su respuesta.

    - clave vigente + mismo body -> `IdempotentResult(replayed=True)` sin ejecutar nada;
    - clave vigente + otro body -> `IdempotencyKeyReusedError` (422);
    - clave vencida -> se borra y se ejecuta de nuevo;
    - sin clave vigente -> se ejecuta y se guarda la respuesta (status y body) en la misma
      transacción. Si la operación falla no se guarda nada.

    `serializer` devuelve el body ya serializado tal cual se responde.
    """
    request_hash = compute_request_hash(request_model)
    filters = _key_filters(user_id, scope, key)
    try:
        await _lock(session, user_id=user_id, scope=scope, key=key)
        stored = (
            await session.execute(
                select(IdempotencyKey).where(
                    *filters,
                    IdempotencyKey.expires_at > func.now(),
                )
            )
        ).scalar_one_or_none()
        if stored is not None:
            if stored.request_hash != request_hash:
                raise IdempotencyKeyReusedError(KEY_REUSED_DETAIL)
            logger.info("idempotency_replay scope=%s user_id=%s", scope, user_id)
            return IdempotentResult(
                status_code=stored.status_code,
                body=stored.response_body,
                replayed=True,
            )

        # Sin clave vigente: se descarta la fila existente (vencida) y se ejecuta la operación.
        await session.execute(delete(IdempotencyKey).where(*filters))
        result = await operation()
        body = serializer(result)
        await _store(
            session,
            user_id=user_id,
            scope=scope,
            key=key,
            request_hash=request_hash,
            status_code=status_code,
            body=body,
        )
        await session.commit()
    except Exception:
        await session.rollback()
        raise

    logger.info("idempotency_stored scope=%s user_id=%s", scope, user_id)
    return IdempotentResult(status_code=status_code, body=body, replayed=False)
