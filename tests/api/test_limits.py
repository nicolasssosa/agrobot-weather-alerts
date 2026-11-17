"""Tests de los límites operativos de S14a.

Ver docs/BACKLOG.md §S14a, docs/API.md §Convenciones y docs/TESTING.md
§`tests/api/test_limits.py`:

- tamaño máximo del body (`MAX_REQUEST_BODY_BYTES`): 413 con y sin `Content-Length`;
- 422 acotado a 20 errores + resumen;
- `lock_timeout` / `statement_timeout` -> 503 `{"detail": "Database busy, try again later"}`
  con `Retry-After: 1`.
"""

import logging
import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import delete, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request

from app.api.middleware import MaxRequestBodySizeMiddleware
from app.core.config import Settings, get_settings
from app.core.db import build_engine, get_session
from app.core.errors import _db_timeout_handler
from app.main import create_app
from app.models import User
from tests.conftest import TEST_DATABASE_URL, MakeUser, auth

FIELDS = "/api/v1/fields"
USERS = "/api/v1/users"
DB_BUSY = "Database busy, try again later"


def _field_body(size: int) -> bytes:
    """Body JSON de exactamente `size` bytes (el `name` se rellena para llegar al tamaño)."""
    prefix = b'{"name": "'
    suffix = b'", "latitude": -34.6, "longitude": -58.4}'
    padding = size - len(prefix) - len(suffix)
    assert padding >= 0
    return prefix + b"x" * padding + suffix


async def _chunks(body: bytes, *, chunk_size: int = 8192) -> AsyncIterator[bytes]:
    """Generador async: httpx lo manda como chunked (sin `Content-Length`)."""
    for start in range(0, len(body), chunk_size):
        yield body[start : start + chunk_size]


# --- Tamaño máximo del body (MAX_REQUEST_BODY_BYTES) -------------------------


async def test_body_one_byte_over_the_limit_returns_413(
    client: AsyncClient, make_user: MakeUser, caplog: pytest.LogCaptureFixture
) -> None:
    """`Content-Length` mayor al límite: 413 con el detail exacto, sin procesar el body."""
    caplog.set_level(logging.WARNING, logger="app.api.middleware")
    user = await make_user()
    limit = Settings().max_request_body_bytes

    response = await client.post(
        FIELDS,
        content=_field_body(limit + 1),
        headers={**auth(user), "content-type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "Request body too large"}
    assert f"request_body_too_large path={FIELDS} limit={limit}" in [
        record.getMessage() for record in caplog.records
    ]


async def test_body_exactly_at_the_limit_is_not_rejected_by_the_middleware(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """Exactamente el límite pasa el middleware (el schema responde 422, no 413)."""
    user = await make_user()
    limit = Settings().max_request_body_bytes

    response = await client.post(
        FIELDS,
        content=_field_body(limit),
        headers={**auth(user), "content-type": "application/json"},
    )

    assert response.status_code == 422
    assert len(response.request.content) == limit


async def test_a_chunked_body_over_the_limit_returns_413(
    client: AsyncClient, make_user: MakeUser, caplog: pytest.LogCaptureFixture
) -> None:
    """Sin `Content-Length` (chunked) se corta al superar el límite, con el mismo 413."""
    caplog.set_level(logging.WARNING, logger="app.api.middleware")
    user = await make_user()
    limit = Settings().max_request_body_bytes
    body = _field_body(limit + 1)

    response = await client.post(
        FIELDS,
        content=_chunks(body),
        headers={**auth(user), "content-type": "application/json"},
    )

    assert "content-length" not in {name.lower() for name in response.request.headers}
    assert response.status_code == 413
    assert response.json() == {"detail": "Request body too large"}
    assert any("request_body_too_large" in record.getMessage() for record in caplog.records)


async def test_a_small_chunked_body_still_reaches_the_endpoint(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """Un body chunked chico sigue el camino normal: el middleware no rompe el streaming."""
    user = await make_user()
    body = b'{"name": "Lote chunked", "latitude": -34.6, "longitude": -58.4}'

    response = await client.post(
        FIELDS, content=_chunks(body), headers={**auth(user), "content-type": "application/json"}
    )

    assert response.status_code == 201
    assert response.json()["name"] == "Lote chunked"


# --- Casos borde del middleware (ASGI puro) ---------------------------------


def _http_scope(*, headers: dict[bytes, bytes], path: str = FIELDS) -> dict[str, object]:
    """Scope ASGI de un POST a `path` con los headers indicados (nombres en minúscula)."""
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": list(headers.items()),
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
    }


async def _run_middleware(
    max_body_bytes: int,
    *,
    headers: dict[bytes, bytes],
    body: bytes,
    chunk_size: int = 1024,
) -> tuple[list[int], int]:
    """Corre el middleware con un app ASGI que lee todo el body y responde 200.

    Devuelve `(statuses enviados, bytes que el app alcanzó a leer)`: si el middleware corta,
    el único status es el 413 y el app no llega a responder.
    """
    pending = [body[index : index + chunk_size] for index in range(0, len(body), chunk_size)]
    pending = pending or [b""]
    read = 0

    async def receive() -> dict[str, object]:
        chunk = pending.pop(0)
        return {"type": "http.request", "body": chunk, "more_body": bool(pending)}

    async def app(scope: dict[str, object], receive, send) -> None:
        nonlocal read
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            read += len(message.get("body", b""))
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    statuses: list[int] = []

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.start":
            statuses.append(int(message["status"]))

    middleware = MaxRequestBodySizeMiddleware(app, max_body_bytes=max_body_bytes)
    await middleware(_http_scope(headers=headers), receive, send)
    return statuses, read


async def test_a_valid_content_length_over_the_limit_is_rejected_without_reading_the_body() -> None:
    """Con `Content-Length` declarado no se lee nada del body: el 413 sale del header."""
    statuses, read = await _run_middleware(16, headers={b"content-length": b"17"}, body=b"x" * 17)

    assert statuses == [413]
    assert read == 0


async def test_a_lying_content_length_does_not_bypass_the_limit() -> None:
    """Un `Content-Length` que subdeclara el body no saltea el tope: se cuentan los bytes."""
    statuses, read = await _run_middleware(
        16, headers={b"content-length": b"16"}, body=b"x" * 17, chunk_size=8
    )

    assert statuses == [413]
    assert 0 < read <= 16


@pytest.mark.parametrize("declared", [b"abc", b"-1", b"12.5", b""])
async def test_an_invalid_content_length_does_not_bypass_the_limit(declared: bytes) -> None:
    """Un `Content-Length` inválido o negativo no saltea el tope: se cuentan los bytes."""
    statuses, read = await _run_middleware(
        16, headers={b"content-length": declared}, body=b"x" * 17, chunk_size=8
    )

    assert statuses == [413]
    # El app no llega a leer más que el límite: el chunk que lo supera se corta.
    assert 0 < read <= 16


async def test_a_chunked_body_summing_exactly_the_limit_gets_through() -> None:
    """Varios chunks que suman justo el límite: el middleware no corta en el borde."""
    statuses, read = await _run_middleware(
        16, headers={b"transfer-encoding": b"chunked"}, body=b"x" * 16, chunk_size=4
    )

    assert statuses == [200]
    assert read == 16


async def test_a_non_http_scope_is_forwarded_untouched() -> None:
    """`lifespan` (u otro scope no-HTTP) pasa al app sin contar bytes ni responder nada."""
    seen: list[str] = []

    async def app(scope: dict[str, object], receive, send) -> None:
        seen.append(str(scope["type"]))

    async def receive() -> dict[str, object]:
        raise AssertionError("el middleware no debe leer el body de un scope no-HTTP")

    async def send(message: dict[str, object]) -> None:
        raise AssertionError("el app de prueba no envía nada")

    middleware = MaxRequestBodySizeMiddleware(app, max_body_bytes=16)
    await middleware({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)

    assert seen == ["lifespan"]


async def test_a_get_without_a_body_is_not_affected(client: AsyncClient) -> None:
    """Los métodos sin body pasan el middleware sin tocar nada (`GET /health` -> 200)."""
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_the_body_limit_applies_to_every_endpoint(client: AsyncClient) -> None:
    """El tope no es de `/fields`: cualquier endpoint responde el mismo 413 (S14a)."""
    limit = Settings().max_request_body_bytes
    body = b'{"name": "' + b"x" * (limit + 1) + b'"}'

    response = await client.post(USERS, content=body, headers={"content-type": "application/json"})

    assert response.status_code == 413
    assert response.json() == {"detail": "Request body too large"}


async def test_the_app_uses_the_configured_body_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """El límite registrado en la app sale de `MAX_REQUEST_BODY_BYTES`, no de una constante."""
    monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", "2048")
    get_settings.cache_clear()
    try:
        app = create_app()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as unauthenticated:
            response = await unauthenticated.post(
                FIELDS,
                content=b"x" * 2049,
                headers={"content-type": "application/json"},
            )
    finally:
        # Otra vez los defaults: el caché de settings no debe quedar con el límite del test.
        get_settings.cache_clear()

    assert response.status_code == 413
    assert response.json() == {"detail": "Request body too large"}


# --- 422 acotado a 20 errores ------------------------------------------------


def _user_body_with_extra_keys(extra: int) -> dict[str, object]:
    """Body válido de `POST /users` con `extra` claves desconocidas (`extra_forbidden`)."""
    return {
        "phone_number": "+5491122334455",
        "name": "Ana",
        **{f"extra_{index}": index for index in range(extra)},
    }


async def test_more_than_20_validation_errors_are_capped(client: AsyncClient) -> None:
    """25 errores -> 20 + `{"loc": [], "msg": "5 more errors omitted", ...}`."""
    response = await client.post(USERS, json=_user_body_with_extra_keys(25))

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert len(detail) == 21
    assert all(error["type"] == "extra_forbidden" for error in detail[:20])
    assert detail[20] == {
        "loc": [],
        "msg": "5 more errors omitted",
        "type": "too_many_errors",
    }


async def test_exactly_21_validation_errors_omit_a_single_one(client: AsyncClient) -> None:
    """21 errores -> 20 + `1 more errors omitted` (el borde del tope)."""
    response = await client.post(USERS, json=_user_body_with_extra_keys(21))

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert len(detail) == 21
    assert detail[20] == {
        "loc": [],
        "msg": "1 more errors omitted",
        "type": "too_many_errors",
    }


async def test_20_or_fewer_validation_errors_are_returned_unchanged(client: AsyncClient) -> None:
    """Con exactamente 20 errores no se agrega el resumen: la respuesta no cambia."""
    response = await client.post(USERS, json=_user_body_with_extra_keys(20))

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert len(detail) == 20
    assert all(error["type"] == "extra_forbidden" for error in detail)
    assert "too_many_errors" not in {error["type"] for error in detail}


async def test_a_single_validation_error_keeps_the_fastapi_shape(client: AsyncClient) -> None:
    """El tope no altera los 422 de siempre (docs/API.md §Errores)."""
    response = await client.post(USERS, json={"phone_number": "abc", "name": "Ana"})

    assert response.status_code == 422
    [error] = response.json()["detail"]
    assert error["loc"] == ["body", "phone_number"]
    assert error["type"] != "too_many_errors"


# --- Base de datos ocupada (503) --------------------------------------------


def _app_with_engine(engine) -> FastAPI:  # type: ignore[no-untyped-def]
    """App con las sesiones atadas a un engine propio (timeouts cortos para el test)."""
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    app = create_app()

    async def _override() -> AsyncIterator[object]:
        async with sessionmaker() as session:
            yield session

    app.dependency_overrides[get_session] = _override
    return app


async def _committed_user(engine) -> int:  # type: ignore[no-untyped-def]
    """Crea y commitea un usuario en la base de test (lo ven todas las conexiones)."""
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    async with sessionmaker() as session:
        user = User(phone_number=f"+54911{uuid.uuid4().int % 10**8:08d}", name=f"Limits {suffix}")
        session.add(user)
        await session.commit()
        return user.id


async def _cleanup(engine, user_id: int) -> None:  # type: ignore[no-untyped-def]
    async with engine.begin() as conn:
        await conn.execute(delete(User).where(User.id == user_id))
    await engine.dispose()


async def _post_field_while_locked(
    *, lock_timeout_ms: int, statement_timeout_ms: int, caplog: pytest.LogCaptureFixture
) -> Response:
    """Retiene un lock sobre la fila del usuario y hace `POST /fields` con timeouts cortos."""
    caplog.set_level(logging.WARNING, logger="app.core.errors")
    holder_engine = build_engine(
        Settings(database_url=TEST_DATABASE_URL, db_lock_timeout_ms=0, db_statement_timeout_ms=0)
    )
    app_engine = build_engine(
        Settings(
            database_url=TEST_DATABASE_URL,
            db_lock_timeout_ms=lock_timeout_ms,
            db_statement_timeout_ms=statement_timeout_ms,
            db_pool_size=2,
        )
    )
    user_id = await _committed_user(holder_engine)
    try:
        app = _app_with_engine(app_engine)
        async with holder_engine.connect() as holder:
            # Lock exclusivo sobre la fila que `create_field` bloquea (FOR NO KEY UPDATE).
            await holder.execute(
                text("SELECT id FROM users WHERE id = :user_id FOR UPDATE"),
                {"user_id": user_id},
            )
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    FIELDS,
                    json={"name": "Lote bloqueado", "latitude": -34.6, "longitude": -58.4},
                    headers={"X-User-Id": str(user_id)},
                )
            await holder.rollback()
        return response
    finally:
        await _cleanup(holder_engine, user_id)
        await app_engine.dispose()


async def test_lock_timeout_returns_503(caplog: pytest.LogCaptureFixture) -> None:
    """`lock_timeout` (sqlstate 55P03) -> 503 con `Retry-After: 1` y log `db_timeout`."""
    response = await _post_field_while_locked(
        lock_timeout_ms=200, statement_timeout_ms=30000, caplog=caplog
    )

    assert response.status_code == 503
    assert response.json() == {"detail": DB_BUSY}
    assert response.headers["Retry-After"] == "1"
    messages = [record.getMessage() for record in caplog.records]
    assert f"db_timeout sqlstate=55P03 path={FIELDS}" in messages


async def test_statement_timeout_returns_503(caplog: pytest.LogCaptureFixture) -> None:
    """`statement_timeout` (sqlstate 57014) -> el mismo 503 (sin `lock_timeout`)."""
    response = await _post_field_while_locked(
        lock_timeout_ms=0, statement_timeout_ms=100, caplog=caplog
    )

    assert response.status_code == 503
    assert response.json() == {"detail": DB_BUSY}
    assert response.headers["Retry-After"] == "1"
    messages = [record.getMessage() for record in caplog.records]
    assert f"db_timeout sqlstate=57014 path={FIELDS}" in messages


async def test_other_database_errors_keep_the_current_behaviour(
    db_session: AsyncSession, make_user: MakeUser
) -> None:
    """Un error de DB que no es timeout no se convierte en 503: el handler lo re-lanza."""
    user = await make_user()
    db_session.add(User(phone_number=user.phone_number, name="Duplicado"))
    with pytest.raises(IntegrityError) as error:
        await db_session.flush()
    await db_session.rollback()

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": USERS,
            "headers": [],
            "query_string": b"",
        }
    )

    with pytest.raises(IntegrityError):
        await _db_timeout_handler(request, error.value)


async def test_another_sqlstate_is_not_turned_into_a_503(client: AsyncClient) -> None:
    """`23505` (unicidad) sigue siendo un 409 donde corresponde, no un 503 de timeout."""
    payload = {
        "phone_number": f"+54911{uuid.uuid4().int % 10**8:08d}",
        "name": "Duplicada",
    }
    assert (await client.post(USERS, json=payload)).status_code == 201

    response = await client.post(USERS, json=payload)

    assert response.status_code == 409
    assert response.json() == {"detail": "User with this phone number already exists"}
    assert "Retry-After" not in response.headers
