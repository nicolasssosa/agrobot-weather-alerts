"""Tests de `Idempotency-Key` en los POST (S10). Ver docs/API.md §Idempotency-Key.

Cubre, para los tres endpoints que la soportan (`POST /users`, `/fields`, `/alerts`): primera
vez, replay, body distinto, clave inválida, operación fallida, clave vencida, alcance por
usuario/endpoint, concurrencia real y atomicidad entre el recurso y la clave guardada.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.api.deps import validate_idempotency_key
from app.core.db import get_session
from app.main import create_app
from app.models import Alert, Field, IdempotencyKey, User
from app.schemas.alert import AlertCreate
from app.schemas.common import BIGINT_MAX
from app.schemas.field import FieldCreate
from app.schemas.user import UserCreate
from app.services import idempotency as idempotency_service
from app.services.idempotency import (
    INVALID_KEY_DETAIL,
    KEY_REUSED_DETAIL,
    compute_request_hash,
)
from tests.conftest import TEST_DATABASE_URL, MakeAlert, MakeField, MakeUser, auth

ROOT = Path(__file__).resolve().parents[2]
API = "/api/v1"
USERS = f"{API}/users"
FIELDS = f"{API}/fields"
ALERTS = f"{API}/alerts"
KEY_HEADER = "Idempotency-Key"
REPLAY_HEADER = "Idempotent-Replayed"

CASE_NAMES = ["users", "fields", "alerts"]


@dataclass(frozen=True)
class Case:
    """Un endpoint con un body válido, otro body válido distinto y su dueño."""

    name: str
    path: str
    scope: str
    headers: dict[str, str]
    body: dict[str, Any]
    fixed_body: dict[str, Any]
    user_id: int | None
    model: type[BaseModel]


@pytest.fixture
async def cases(make_user: MakeUser, make_field: MakeField) -> dict[str, Case]:
    user = await make_user()
    own_field = await make_field(user=user)

    return {
        "users": Case(
            name="users",
            path=USERS,
            scope="POST /api/v1/users",
            headers={},
            body={"phone_number": f"+54{uuid4().int % 10**12:012d}", "name": "Idem User"},
            fixed_body={"phone_number": f"+54{uuid4().int % 10**12:012d}", "name": "Idem User"},
            user_id=None,
            model=UserCreate,
        ),
        "fields": Case(
            name="fields",
            path=FIELDS,
            scope="POST /api/v1/fields",
            headers=auth(user),
            body={"name": "Idem Field", "latitude": -34.6, "longitude": -58.4},
            fixed_body={"name": "Idem Field Fixed", "latitude": -34.6, "longitude": -58.4},
            user_id=user.id,
            model=FieldCreate,
        ),
        "alerts": Case(
            name="alerts",
            path=ALERTS,
            scope="POST /api/v1/alerts",
            headers=auth(user),
            body={"field_id": own_field.id, "event_type": "frost", "threshold": 70},
            fixed_body={"field_id": own_field.id, "event_type": "frost", "threshold": 80},
            user_id=user.id,
            model=AlertCreate,
        ),
    }


@dataclass
class CommittedEnv:
    """Cliente con user/field commiteados y sesiones propias (para casos con rollback)."""

    client: AsyncClient
    sessionmaker: async_sessionmaker[AsyncSession]
    user_id: int
    field_id: int
    phone_number: str


@pytest.fixture
async def committed_env() -> AsyncIterator[CommittedEnv]:
    """Datos commiteados fuera de la transacción del test.

    Un 409/404 de la API hace `rollback` de su transacción; con `client`/`db_session` ese
    rollback descartaría los datos del fixture del test (misma sesión). Acá cada request usa
    su propia sesión contra datos ya commiteados, y el `finally` limpia siempre.
    """
    engine: AsyncEngine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    phone_number = f"+54{uuid4().int % 10**12:012d}"
    try:
        async with sessionmaker() as session:
            user = User(phone_number=phone_number, name="Idem Retry")
            session.add(user)
            await session.flush()
            field = Field(
                user_id=user.id,
                name="Idem Retry Field",
                latitude=Decimal("-34.6"),
                longitude=Decimal("-58.4"),
            )
            session.add(field)
            await session.commit()
            user_id = user.id
            field_id = field.id

        app = create_app()

        async def _override_session() -> AsyncIterator[AsyncSession]:
            async with sessionmaker() as session:
                yield session

        app.dependency_overrides[get_session] = _override_session
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield CommittedEnv(
                client=client,
                sessionmaker=sessionmaker,
                user_id=user_id,
                field_id=field_id,
                phone_number=phone_number,
            )
    finally:
        async with sessionmaker() as session:
            await session.execute(delete(User).where(User.phone_number == phone_number))
            await session.commit()
        await engine.dispose()


async def _post(
    client: AsyncClient,
    case: Case,
    key: str,
    *,
    body: dict[str, Any] | None = None,
) -> Any:
    return await client.post(
        case.path,
        json=case.body if body is None else body,
        headers={**case.headers, KEY_HEADER: key},
    )


async def _stored_keys(session: AsyncSession, case: Case, key: str) -> list[IdempotencyKey]:
    # En `POST /users` el scope incluye el hash del body (SEC-1): se compara por prefijo.
    result = await session.execute(
        select(IdempotencyKey).where(
            IdempotencyKey.scope.startswith(case.scope), IdempotencyKey.key == key
        )
    )
    return list(result.scalars().all())


async def _resource_count(session: AsyncSession, case: Case) -> int:
    if case.name == "users":
        query = (
            select(func.count())
            .select_from(User)
            .where(User.phone_number == case.body["phone_number"])
        )
    elif case.name == "fields":
        query = (
            select(func.count())
            .select_from(Field)
            .where(Field.user_id == case.user_id, Field.name == case.body["name"])
        )
    else:
        query = (
            select(func.count())
            .select_from(Alert)
            .where(
                Alert.field_id == case.body["field_id"],
                Alert.event_type == case.body["event_type"],
                Alert.threshold == case.body["threshold"],
            )
        )
    return (await session.execute(query)).scalar_one()


async def _delete_resource(session: AsyncSession, case: Case, resource_id: int) -> None:
    if case.name == "users":
        await session.execute(delete(User).where(User.phone_number == case.body["phone_number"]))
    elif case.name == "fields":
        await session.execute(delete(Field).where(Field.id == resource_id))
    else:
        await session.execute(delete(Alert).where(Alert.id == resource_id))


def _client_with_committed_session(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncClient:
    """Cliente ASGI con una sesión por request contra datos ya commiteados.

    Necesario cuando una request puede hacer `rollback` (409/404) o cuando se lanzan
    requests concurrentes: la `db_session` del test es una única conexión.
    """
    app = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    app.dependency_overrides[get_session] = _override_session
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _stored_key_count(session: AsyncSession, scope: str, key: str) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(IdempotencyKey)
            .where(IdempotencyKey.scope.startswith(scope), IdempotencyKey.key == key)
        )
    ).scalar_one()


# --- Primera vez y replay -----------------------------------------------------


@pytest.mark.parametrize("case_name", CASE_NAMES)
async def test_without_the_header_nothing_is_stored(
    client: AsyncClient, db_session: AsyncSession, cases: dict[str, Case], case_name: str
) -> None:
    """Sin header el comportamiento es el de siempre: 201 y ninguna fila de idempotencia."""
    case = cases[case_name]

    response = await client.post(case.path, json=case.body, headers=case.headers)

    assert response.status_code == 201
    assert REPLAY_HEADER not in response.headers
    stored = await db_session.scalar(select(func.count()).select_from(IdempotencyKey))
    assert stored == 0


@pytest.mark.parametrize("case_name", CASE_NAMES)
async def test_first_post_with_a_key_creates_and_stores_the_response(
    client: AsyncClient, db_session: AsyncSession, cases: dict[str, Case], case_name: str
) -> None:
    case = cases[case_name]

    response = await _post(client, case, "first-key")

    assert response.status_code == 201
    assert REPLAY_HEADER not in response.headers
    assert await _resource_count(db_session, case) == 1
    stored = await _stored_keys(db_session, case, "first-key")
    assert len(stored) == 1
    assert stored[0].status_code == 201
    if case.name == "users":
        # SEC-2: en `POST /users` se guarda solo el id, sin PII.
        assert json.loads(stored[0].response_body) == {"id": response.json()["id"]}
    else:
        # CR-1: TEXT con los mismos bytes que la respuesta.
        assert stored[0].response_body == response.text
        assert json.loads(stored[0].response_body) == response.json()
    assert stored[0].request_hash == compute_request_hash(case.model.model_validate(case.body))
    assert stored[0].scope.startswith(case.scope)
    assert stored[0].user_id == case.user_id
    assert stored[0].expires_at > datetime.now(UTC)


@pytest.mark.parametrize("case_name", CASE_NAMES)
async def test_replay_returns_the_saved_response_without_a_second_resource(
    client: AsyncClient, db_session: AsyncSession, cases: dict[str, Case], case_name: str
) -> None:
    case = cases[case_name]
    first = await _post(client, case, "replay-key")

    replay = await _post(client, case, "replay-key")

    assert first.status_code == 201
    assert replay.status_code == 201
    assert replay.headers[REPLAY_HEADER] == "true"
    assert replay.json() == first.json()
    # CR-1: el replay devuelve exactamente los mismos bytes que la respuesta original.
    assert replay.content == first.content
    assert await _resource_count(db_session, case) == 1
    assert len(await _stored_keys(db_session, case, "replay-key")) == 1


@pytest.mark.parametrize("case_name", CASE_NAMES)
async def test_replay_ignores_json_key_order_and_whitespace(
    client: AsyncClient, db_session: AsyncSession, cases: dict[str, Case], case_name: str
) -> None:
    case = cases[case_name]
    first = await _post(client, case, "normalized-key")
    reordered = dict(reversed(list(case.body.items())))

    replay = await client.post(
        case.path,
        content=json.dumps(reordered, indent=3),
        headers={
            **case.headers,
            KEY_HEADER: "normalized-key",
            "content-type": "application/json",
        },
    )

    assert first.status_code == 201
    assert replay.status_code == 201
    assert replay.headers[REPLAY_HEADER] == "true"
    assert replay.json() == first.json()
    assert await _resource_count(db_session, case) == 1


@pytest.mark.parametrize("case_name", ["fields", "alerts"])
async def test_the_same_key_with_a_different_body_returns_422(
    client: AsyncClient, db_session: AsyncSession, cases: dict[str, Case], case_name: str
) -> None:
    case = cases[case_name]
    assert (await _post(client, case, "reused-key")).status_code == 201

    conflict = await _post(client, case, "reused-key", body=case.fixed_body)

    assert conflict.status_code == 422
    assert conflict.json() == {"detail": KEY_REUSED_DETAIL}
    assert await _resource_count(db_session, case) == 1


# --- Clave inválida -----------------------------------------------------------


@pytest.mark.parametrize("case_name", CASE_NAMES)
@pytest.mark.parametrize("key", ["", "x" * 256, "with space", "\x7f"])
async def test_invalid_keys_return_422_and_do_not_create_anything(
    client: AsyncClient, db_session: AsyncSession, cases: dict[str, Case], case_name: str, key: str
) -> None:
    case = cases[case_name]

    response = await _post(client, case, key)

    assert response.status_code == 422
    assert response.json() == {"detail": INVALID_KEY_DETAIL}
    assert await _resource_count(db_session, case) == 0
    assert await _stored_keys(db_session, case, key) == []


@pytest.mark.parametrize("key", ["café", "ñ", "\u0661", "tab\tchar"])
def test_non_ascii_or_control_keys_are_rejected(key: str) -> None:
    # HTTPX no puede transportar un header no ASCII (`UnicodeEncodeError`), así que el caso
    # se cubre directo sobre el validador de la dependency.
    with pytest.raises(HTTPException) as exc_info:
        validate_idempotency_key(key)

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == INVALID_KEY_DETAIL


# --- Errores y vencimiento ----------------------------------------------------


@pytest.mark.parametrize("case_name", CASE_NAMES)
async def test_a_failed_operation_does_not_store_the_key_and_the_retry_works(
    committed_env: CommittedEnv, case_name: str
) -> None:
    env = committed_env
    key = "retry-key"
    if case_name == "users":
        path, headers = USERS, {}
        failing = {"phone_number": env.phone_number, "name": "Retry User"}
        fixed = {"phone_number": f"+54{uuid4().int % 10**12:012d}", "name": "Retry User"}
        expected_status = 409
    elif case_name == "fields":
        path, headers = FIELDS, {"X-User-Id": str(env.user_id)}
        failing = {"name": "Idem Retry Field", "latitude": -34.6, "longitude": -58.4}
        fixed = {"name": "Idem Retry Field Fixed", "latitude": -34.6, "longitude": -58.4}
        expected_status = 409
    else:
        path, headers = ALERTS, {"X-User-Id": str(env.user_id)}
        failing = {"field_id": BIGINT_MAX, "event_type": "frost", "threshold": 70}
        fixed = {"field_id": env.field_id, "event_type": "frost", "threshold": 70}
        expected_status = 404

    try:
        failed = await env.client.post(path, json=failing, headers={**headers, KEY_HEADER: key})

        assert failed.status_code == expected_status
        async with env.sessionmaker() as session:
            stored = await session.scalar(
                select(func.count()).select_from(IdempotencyKey).where(IdempotencyKey.key == key)
            )
        assert stored == 0

        retried = await env.client.post(path, json=fixed, headers={**headers, KEY_HEADER: key})

        assert retried.status_code == 201
        assert REPLAY_HEADER not in retried.headers
        async with env.sessionmaker() as session:
            stored = await session.scalar(
                select(func.count()).select_from(IdempotencyKey).where(IdempotencyKey.key == key)
            )
        assert stored == 1
    finally:
        async with env.sessionmaker() as session:
            if case_name == "users":
                await session.execute(
                    delete(User).where(User.phone_number == fixed["phone_number"])
                )
            await session.execute(delete(IdempotencyKey).where(IdempotencyKey.key == key))
            await session.commit()


@pytest.mark.parametrize("case_name", CASE_NAMES)
async def test_an_expired_key_is_treated_as_new(
    client: AsyncClient, db_session: AsyncSession, cases: dict[str, Case], case_name: str
) -> None:
    case = cases[case_name]
    first = await _post(client, case, "expired-key")
    assert first.status_code == 201
    await db_session.execute(
        update(IdempotencyKey)
        .where(IdempotencyKey.scope.startswith(case.scope), IdempotencyKey.key == "expired-key")
        .values(expires_at=datetime(2020, 1, 1, tzinfo=UTC))
    )
    db_session.expire_all()
    await _delete_resource(db_session, case, first.json()["id"])
    assert await _resource_count(db_session, case) == 0

    second = await _post(client, case, "expired-key")

    assert second.status_code == 201
    assert REPLAY_HEADER not in second.headers
    assert second.json()["id"] != first.json()["id"]
    assert await _resource_count(db_session, case) == 1


# --- Alcance de la clave ------------------------------------------------------


async def test_the_key_is_scoped_to_the_user(client: AsyncClient, make_user: MakeUser) -> None:
    first_user = await make_user()
    second_user = await make_user()
    body = {"name": "Shared Name", "latitude": -34.6, "longitude": -58.4}

    first = await client.post(
        FIELDS, json=body, headers={**auth(first_user), KEY_HEADER: "shared-key"}
    )
    second = await client.post(
        FIELDS, json=body, headers={**auth(second_user), KEY_HEADER: "shared-key"}
    )

    assert first.status_code == second.status_code == 201
    assert REPLAY_HEADER not in second.headers
    assert first.json()["id"] != second.json()["id"]

    replay = await client.post(
        FIELDS, json=body, headers={**auth(first_user), KEY_HEADER: "shared-key"}
    )
    assert replay.headers[REPLAY_HEADER] == "true"
    assert replay.json() == first.json()


async def test_the_key_is_scoped_to_the_endpoint(
    client: AsyncClient, db_session: AsyncSession, cases: dict[str, Case]
) -> None:
    user_case, field_case = cases["users"], cases["fields"]

    user_response = await _post(client, user_case, "cross-endpoint")
    field_response = await _post(client, field_case, "cross-endpoint")

    assert user_response.status_code == field_response.status_code == 201
    assert REPLAY_HEADER not in field_response.headers
    assert len(await _stored_keys(db_session, user_case, "cross-endpoint")) == 1
    assert len(await _stored_keys(db_session, field_case, "cross-endpoint")) == 1


# --- `POST /users` sin auth: alcance por body y sin PII ------------------------


async def test_post_users_with_the_same_key_and_different_bodies_creates_both(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """SEC-1: dos clientes anónimos con la misma clave y bodies distintos -> dos altas (201).

    El scope de `POST /users` incluye el hash del body, así que una clave ajena no puede
    bloquear el alta de otro cliente con un 422 durante la vigencia.
    """
    first_body = {"phone_number": f"+54{uuid4().int % 10**12:012d}", "name": "Client A"}
    second_body = {"phone_number": f"+54{uuid4().int % 10**12:012d}", "name": "Client B"}
    key = "anonymous-shared-key"
    app = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as other:
        first = await client.post(USERS, json=first_body, headers={KEY_HEADER: key})
        second = await other.post(USERS, json=second_body, headers={KEY_HEADER: key})
        replay = await other.post(USERS, json=first_body, headers={KEY_HEADER: key})

    assert first.status_code == second.status_code == 201
    assert REPLAY_HEADER not in first.headers
    assert REPLAY_HEADER not in second.headers
    assert first.json()["id"] != second.json()["id"]
    assert first.json()["phone_number"] == first_body["phone_number"]
    assert second.json()["phone_number"] == second_body["phone_number"]

    rows = (
        await db_session.execute(
            select(IdempotencyKey).where(
                IdempotencyKey.scope.startswith("POST /api/v1/users"),
                IdempotencyKey.key == key,
            )
        )
    ).scalars()
    stored = list(rows.all())
    assert len(stored) == 2
    assert len({row.scope for row in stored}) == 2
    assert all(row.user_id is None for row in stored)
    assert all(len(row.scope) <= 64 for row in stored)

    # Mismo body + misma clave (de cualquier cliente) -> replay de su respuesta.
    assert replay.status_code == 201
    assert replay.headers[REPLAY_HEADER] == "true"
    assert replay.content == first.content


async def test_post_users_does_not_store_pii_and_the_replay_is_byte_exact(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """SEC-2: la fila guarda solo `{"id": …}`; el replay reconstruye el body completo."""
    phone_number = f"+54{uuid4().int % 10**12:012d}"
    body = {"phone_number": phone_number, "name": "QA No PII"}

    first = await client.post(USERS, json=body, headers={KEY_HEADER: "no-pii-key"})
    stored = (
        await db_session.execute(select(IdempotencyKey).where(IdempotencyKey.key == "no-pii-key"))
    ).scalar_one()

    assert first.status_code == 201
    assert json.loads(stored.response_body) == {"id": first.json()["id"]}
    assert phone_number not in stored.response_body
    assert "QA No PII" not in stored.response_body

    replay = await client.post(USERS, json=body, headers={KEY_HEADER: "no-pii-key"})

    assert replay.status_code == 201
    assert replay.headers[REPLAY_HEADER] == "true"
    assert replay.content == first.content
    assert replay.json() == first.json()
    assert replay.json()["phone_number"] == phone_number


async def test_post_users_replay_after_the_user_was_deleted_executes_again(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """SEC-2: si el usuario guardado ya no existe, la fila se borra y la clave se re-ejecuta."""
    phone_number = f"+54{uuid4().int % 10**12:012d}"
    body = {"phone_number": phone_number, "name": "QA Recreated"}
    key = "recreated-user-key"

    first = await client.post(USERS, json=body, headers={KEY_HEADER: key})
    first_id = first.json()["id"]
    await db_session.execute(delete(User).where(User.id == first_id))
    db_session.expire_all()

    second = await client.post(USERS, json=body, headers={KEY_HEADER: key})

    assert second.status_code == 201
    assert REPLAY_HEADER not in second.headers
    assert second.json()["id"] != first_id
    created = await db_session.scalar(
        select(func.count()).select_from(User).where(User.phone_number == phone_number)
    )
    assert created == 1
    stored = (
        await db_session.execute(select(IdempotencyKey).where(IdempotencyKey.key == key))
    ).scalar_one()
    assert json.loads(stored.response_body) == {"id": second.json()["id"]}


# --- Concurrencia y atomicidad ------------------------------------------------


async def test_concurrent_posts_with_the_same_key_create_exactly_one_resource() -> None:
    """Dos POST simultáneos con la misma clave: un solo recurso y un solo replay.

    No sirve `client`/`db_session`: esa sesión está atada a una única conexión (no admite
    requests concurrentes) y su transacción se revierte al final, así que otra conexión no
    vería los datos. Como en docs/TESTING.md §Concurrencia se usa un engine propio (NullPool),
    datos commiteados y limpieza incondicional en el `finally`.
    """
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    phone_number = f"+54{uuid4().int % 10**12:012d}"
    key = f"concurrent-{uuid4()}"
    try:
        async with sessionmaker() as session:
            user = User(phone_number=phone_number, name="QA Idempotency")
            session.add(user)
            await session.commit()
            user_id = user.id

        def build_client() -> AsyncClient:
            app = create_app()

            async def _override_session() -> AsyncIterator[AsyncSession]:
                async with sessionmaker() as session:
                    yield session

            app.dependency_overrides[get_session] = _override_session
            return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

        payload = {"name": "Concurrent Field", "latitude": -34.6, "longitude": -58.4}
        headers = {"X-User-Id": str(user_id), KEY_HEADER: key}
        async with build_client() as first, build_client() as second:
            first_response, second_response = await asyncio.gather(
                first.post(FIELDS, json=payload, headers=headers),
                second.post(FIELDS, json=payload, headers=headers),
            )

        assert {first_response.status_code, second_response.status_code} == {201}
        assert first_response.json() == second_response.json()
        replayed = [
            response.headers.get(REPLAY_HEADER) == "true"
            for response in (first_response, second_response)
        ]
        assert replayed.count(True) == 1

        async with sessionmaker() as session:
            resources = await session.scalar(
                select(func.count()).select_from(Field).where(Field.user_id == user_id)
            )
            keys = await session.scalar(
                select(func.count())
                .select_from(IdempotencyKey)
                .where(IdempotencyKey.user_id == user_id, IdempotencyKey.key == key)
            )
        assert resources == 1
        assert keys == 1
    finally:
        async with sessionmaker() as session:
            await session.execute(delete(User).where(User.phone_number == phone_number))
            await session.commit()
        await engine.dispose()


async def test_a_failed_key_insert_rolls_back_the_resource(
    committed_env: CommittedEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = committed_env
    phone_number = f"+54{uuid4().int % 10**12:012d}"
    key = "atomic-key"

    async def _boom(*_: object, **__: object) -> None:
        raise RuntimeError("idempotency store failed")

    try:
        monkeypatch.setattr(idempotency_service, "_store", _boom)
        with pytest.raises(RuntimeError, match="idempotency store failed"):
            await env.client.post(
                USERS,
                json={"phone_number": phone_number, "name": "Atomic User"},
                headers={KEY_HEADER: key},
            )

        # El recurso y la clave se guardan en la misma transacción: si la clave no se pudo
        # guardar, el usuario tampoco quedó (rollback).
        async with env.sessionmaker() as session:
            users = await session.scalar(
                select(func.count()).select_from(User).where(User.phone_number == phone_number)
            )
            keys = await session.scalar(
                select(func.count()).select_from(IdempotencyKey).where(IdempotencyKey.key == key)
            )
        assert users == 0
        assert keys == 0

        monkeypatch.undo()
        retried = await env.client.post(
            USERS,
            json={"phone_number": phone_number, "name": "Atomic User"},
            headers={KEY_HEADER: key},
        )

        assert retried.status_code == 201
        async with env.sessionmaker() as session:
            users = await session.scalar(
                select(func.count()).select_from(User).where(User.phone_number == phone_number)
            )
        assert users == 1
    finally:
        async with env.sessionmaker() as session:
            await session.execute(delete(User).where(User.phone_number == phone_number))
            await session.execute(delete(IdempotencyKey).where(IdempotencyKey.key == key))
            await session.commit()


# --- Límites y caracteres del header -----------------------------------------


@pytest.mark.parametrize("key", ["a", "~", "!", "x" * 255, "A~!" + "z" * 252])
async def test_printable_ascii_keys_at_the_limits_are_accepted_and_replayed(
    client: AsyncClient, db_session: AsyncSession, make_user: MakeUser, key: str
) -> None:
    """1 y 255 caracteres; `~` y `!` (ASCII imprimibles) son claves válidas."""
    user = await make_user()
    body = {"name": f"Boundary {len(key)} {key[0]}", "latitude": -34.6, "longitude": -58.4}

    first = await client.post(FIELDS, json=body, headers={**auth(user), KEY_HEADER: key})

    assert first.status_code == 201
    assert REPLAY_HEADER not in first.headers
    stored = (
        await db_session.execute(
            select(IdempotencyKey).where(
                IdempotencyKey.user_id == user.id, IdempotencyKey.key == key
            )
        )
    ).scalar_one()
    assert stored.key == key

    replay = await client.post(FIELDS, json=body, headers={**auth(user), KEY_HEADER: key})

    assert replay.status_code == 201
    assert replay.headers[REPLAY_HEADER] == "true"
    assert replay.json() == first.json()
    resources = (
        await db_session.execute(
            select(func.count())
            .select_from(Field)
            .where(Field.user_id == user.id, Field.name == body["name"])
        )
    ).scalar_one()
    assert resources == 1


@pytest.mark.parametrize(
    "key",
    ["🌧", "a\nb", " left", "right ", "\u00a0", "a\u200bb", "\ud800"],
)
def test_keys_outside_printable_ascii_are_rejected(key: str) -> None:
    """No-ASCII, espacios en los bordes, controles y surrogates sueltos -> 422."""
    with pytest.raises(HTTPException) as exc_info:
        validate_idempotency_key(key)

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == INVALID_KEY_DETAIL


async def test_a_repeated_header_uses_the_first_value(
    client: AsyncClient, db_session: AsyncSession, make_user: MakeUser
) -> None:
    """Header repetido: el contrato no lo define; se fija el comportamiento actual.

    Starlette/FastAPI toma el primer valor y descarta el resto, así que la clave guardada es
    la primera (docs/API.md §Idempotency-Key habla del header en singular).
    """
    user = await make_user()
    user_id = user.id
    body = {"name": "Repeated Header", "latitude": -34.6, "longitude": -58.4}

    response = await client.post(
        FIELDS,
        json=body,
        headers=[
            ("X-User-Id", str(user_id)),
            (KEY_HEADER, "first-key"),
            (KEY_HEADER, "second-key"),
        ],
    )

    assert response.status_code == 201
    assert REPLAY_HEADER not in response.headers
    stored = (
        await db_session.execute(
            select(IdempotencyKey.key).where(IdempotencyKey.user_id == user_id)
        )
    ).scalars()
    assert list(stored.all()) == ["first-key"]

    # El segundo valor se descartó: no hay clave guardada, así que el mismo body da 409.
    only_second = await client.post(
        FIELDS, json=body, headers={"X-User-Id": str(user_id), KEY_HEADER: "second-key"}
    )
    assert only_second.status_code == 409

    replay = await client.post(
        FIELDS, json=body, headers={"X-User-Id": str(user_id), KEY_HEADER: "first-key"}
    )
    assert replay.status_code == 201
    assert replay.headers[REPLAY_HEADER] == "true"


# --- Validación del body antes que la idempotencia ----------------------------


async def test_an_invalid_body_is_rejected_before_the_key_is_stored(
    client: AsyncClient,
    db_session: AsyncSession,
    make_user: MakeUser,
    make_field: MakeField,
) -> None:
    """Un 422 de Pydantic no guarda la clave: el cliente corrige y reintenta con la misma."""
    user = await make_user()
    field = await make_field(user=user)
    phone_number = f"+54{uuid4().int % 10**12:012d}"
    key = "invalid-body-key"
    cases = [
        (
            USERS,
            {},
            {"phone_number": "no-es-un-telefono", "name": "Ana"},
            {"phone_number": phone_number, "name": "Ana"},
            "POST /api/v1/users",
        ),
        (
            FIELDS,
            auth(user),
            {"name": "Invalid Body", "latitude": 91, "longitude": -58.4},
            {"name": "Invalid Body", "latitude": -34.6, "longitude": -58.4},
            "POST /api/v1/fields",
        ),
        (
            ALERTS,
            auth(user),
            {"field_id": field.id, "event_type": "snow", "threshold": 70},
            {"field_id": field.id, "event_type": "frost", "threshold": 70},
            "POST /api/v1/alerts",
        ),
    ]

    for path, headers, invalid, valid, scope in cases:
        rejected = await client.post(path, json=invalid, headers={**headers, KEY_HEADER: key})

        assert rejected.status_code == 422
        # 422 de Pydantic (lista de errores), no el de la idempotencia (detail plano).
        assert isinstance(rejected.json()["detail"], list)
        assert await _stored_key_count(db_session, scope, key) == 0

        accepted = await client.post(path, json=valid, headers={**headers, KEY_HEADER: key})

        assert accepted.status_code == 201
        assert REPLAY_HEADER not in accepted.headers
        assert await _stored_key_count(db_session, scope, key) == 1


async def test_an_invalid_key_is_rejected_even_when_the_body_is_also_invalid(
    client: AsyncClient, db_session: AsyncSession, make_user: MakeUser
) -> None:
    """Ambos fallan con 422 y no se guarda nada (el orden del `detail` no está documentado)."""
    user = await make_user()

    response = await client.post(
        ALERTS,
        json={"field_id": 1, "event_type": "snow", "threshold": 101},
        headers={**auth(user), KEY_HEADER: "with space"},
    )

    assert response.status_code == 422
    assert await _stored_key_count(db_session, "POST /api/v1/alerts", "with space") == 0


# --- Replay vs. estado actual del recurso -------------------------------------


async def test_replay_returns_the_original_body_after_the_alert_changed(
    client: AsyncClient,
    db_session: AsyncSession,
    make_user: MakeUser,
    make_field: MakeField,
) -> None:
    """El replay devuelve el body guardado, no el estado actual del recurso."""
    user = await make_user()
    field = await make_field(user=user)
    body = {"field_id": field.id, "event_type": "frost", "threshold": 70}

    first = await client.post(ALERTS, json=body, headers={**auth(user), KEY_HEADER: "changed-key"})
    alert_id = first.json()["id"]
    patched = await client.patch(f"{ALERTS}/{alert_id}", json={"threshold": 80}, headers=auth(user))

    replay = await client.post(ALERTS, json=body, headers={**auth(user), KEY_HEADER: "changed-key"})

    assert first.status_code == 201
    assert patched.status_code == 200
    assert patched.json()["threshold"] == 80
    assert replay.status_code == 201
    assert replay.headers[REPLAY_HEADER] == "true"
    assert replay.json() == first.json()
    assert replay.json()["threshold"] == 70

    current = await client.get(f"{ALERTS}/{alert_id}", headers=auth(user))
    assert current.json()["threshold"] == 80


async def test_replay_still_returns_the_saved_201_after_the_alert_is_soft_deleted(
    client: AsyncClient,
    db_session: AsyncSession,
    make_user: MakeUser,
    make_field: MakeField,
) -> None:
    """El DELETE lógico del recurso no invalida la respuesta guardada.

    El replay (misma clave + mismo body) devuelve el 201 original aunque la alerta esté
    borrada. Con una clave nueva el mismo body sí crea otra alerta (S9 permite recrearla).
    """
    user = await make_user()
    field = await make_field(user=user)
    body = {"field_id": field.id, "event_type": "frost", "threshold": 70}

    first = await client.post(ALERTS, json=body, headers={**auth(user), KEY_HEADER: "deleted-key"})
    alert_id = first.json()["id"]
    deleted = await client.delete(f"{ALERTS}/{alert_id}", headers=auth(user))

    replay = await client.post(ALERTS, json=body, headers={**auth(user), KEY_HEADER: "deleted-key"})

    assert deleted.status_code == 204
    assert (await client.get(f"{ALERTS}/{alert_id}", headers=auth(user))).status_code == 404
    assert replay.status_code == 201
    assert replay.headers[REPLAY_HEADER] == "true"
    assert replay.json() == first.json()
    deleted_at = (
        await db_session.execute(select(Alert.deleted_at).where(Alert.id == alert_id))
    ).scalar_one()
    assert deleted_at is not None

    recreated = await client.post(
        ALERTS, json=body, headers={**auth(user), KEY_HEADER: "deleted-key-new"}
    )
    replayed_again = await client.post(
        ALERTS, json=body, headers={**auth(user), KEY_HEADER: "deleted-key"}
    )

    assert recreated.status_code == 201
    assert REPLAY_HEADER not in recreated.headers
    assert recreated.json()["id"] != alert_id
    assert replayed_again.json()["id"] == alert_id
    alive = (
        await db_session.execute(
            select(func.count())
            .select_from(Alert)
            .where(Alert.field_id == field.id, Alert.deleted_at.is_(None))
        )
    ).scalar_one()
    assert alive == 1


# --- Cascada al borrar el usuario ---------------------------------------------


async def test_deleting_the_user_cascades_his_keys() -> None:
    """FK `idempotency_keys.user_id ON DELETE CASCADE` (DATA_MODEL §idempotency_keys).

    Las claves de `/fields` y `/alerts` tienen dueño y se borran con él. La de `POST /users`
    guarda `user_id NULL` (no cascadea) pero sin PII: si el usuario ya no existe, la clave
    se descarta y se ejecuta de nuevo (docs/API.md §Idempotency-Key, SEC-2).
    """
    engine: AsyncEngine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    owner_phone = f"+54{uuid4().int % 10**12:012d}"
    public_phone = f"+54{uuid4().int % 10**12:012d}"
    owned_key = f"cascade-{uuid4()}"
    public_key = f"cascade-public-{uuid4()}"
    try:
        async with sessionmaker() as session:
            owner = User(phone_number=owner_phone, name="QA Cascade")
            session.add(owner)
            await session.commit()
            owner_id = owner.id

        async with _client_with_committed_session(sessionmaker) as client:
            owned = await client.post(
                FIELDS,
                json={"name": "Cascade Field", "latitude": -34.6, "longitude": -58.4},
                headers={"X-User-Id": str(owner_id), KEY_HEADER: owned_key},
            )
            public = await client.post(
                USERS,
                json={"phone_number": public_phone, "name": "QA Public"},
                headers={KEY_HEADER: public_key},
            )

        assert owned.status_code == 201
        assert public.status_code == 201
        async with sessionmaker() as session:
            owned_rows = await session.scalar(
                select(func.count())
                .select_from(IdempotencyKey)
                .where(IdempotencyKey.user_id == owner_id)
            )
            public_row = (
                await session.execute(
                    select(IdempotencyKey).where(IdempotencyKey.key == public_key)
                )
            ).scalar_one()
        assert owned_rows == 1
        assert public_row.user_id is None

        async with sessionmaker() as session:
            await session.execute(delete(User).where(User.id == owner_id))
            await session.commit()
            owned_rows = await session.scalar(
                select(func.count())
                .select_from(IdempotencyKey)
                .where(IdempotencyKey.user_id == owner_id)
            )
            public_rows = await session.scalar(
                select(func.count())
                .select_from(IdempotencyKey)
                .where(IdempotencyKey.key == public_key)
            )
        assert owned_rows == 0
        assert public_rows == 1

        # `POST /users` no tiene dueño: borrar el usuario creado no borra la clave guardada,
        # pero el replay ya no puede reconstruir la respuesta: se ejecuta de nuevo.
        async with sessionmaker() as session:
            await session.execute(delete(User).where(User.phone_number == public_phone))
            await session.commit()
        async with _client_with_committed_session(sessionmaker) as client:
            replay = await client.post(
                USERS,
                json={"phone_number": public_phone, "name": "QA Public"},
                headers={KEY_HEADER: public_key},
            )
        assert replay.status_code == 201
        assert REPLAY_HEADER not in replay.headers
        async with sessionmaker() as session:
            recreated = await session.scalar(
                select(func.count()).select_from(User).where(User.phone_number == public_phone)
            )
            public_rows = await session.scalar(
                select(func.count())
                .select_from(IdempotencyKey)
                .where(IdempotencyKey.key == public_key)
            )
        assert recreated == 1
        assert public_rows == 1
    finally:
        async with sessionmaker() as session:
            await session.execute(
                delete(User).where(User.phone_number.in_([owner_phone, public_phone]))
            )
            await session.execute(
                delete(IdempotencyKey).where(IdempotencyKey.key.in_([owned_key, public_key]))
            )
            await session.commit()
        await engine.dispose()


# --- Concurrencia con bodies distintos ----------------------------------------


async def test_concurrent_posts_with_the_same_key_and_different_bodies_conflict() -> None:
    """Dos requests simultáneos con la misma clave y bodies distintos: uno crea, el otro 422."""
    engine: AsyncEngine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    phone_number = f"+54{uuid4().int % 10**12:012d}"
    key = f"concurrent-different-{uuid4()}"
    try:
        async with sessionmaker() as session:
            user = User(phone_number=phone_number, name="QA Concurrent")
            session.add(user)
            await session.flush()
            field = Field(
                user_id=user.id,
                name="QA Concurrent Field",
                latitude=Decimal("-34.6"),
                longitude=Decimal("-58.4"),
            )
            session.add(field)
            await session.commit()
            user_id = user.id
            field_id = field.id

        headers = {"X-User-Id": str(user_id), KEY_HEADER: key}
        bodies = [
            {"field_id": field_id, "event_type": "hail", "threshold": 60},
            {"field_id": field_id, "event_type": "hail", "threshold": 61},
        ]
        async with (
            _client_with_committed_session(sessionmaker) as first,
            _client_with_committed_session(sessionmaker) as second,
        ):
            responses = await asyncio.gather(
                first.post(ALERTS, json=bodies[0], headers=headers),
                second.post(ALERTS, json=bodies[1], headers=headers),
            )

        assert sorted(response.status_code for response in responses) == [201, 422]
        conflict = next(r for r in responses if r.status_code == 422)
        created = next(r for r in responses if r.status_code == 201)
        assert conflict.json() == {"detail": KEY_REUSED_DETAIL}
        assert REPLAY_HEADER not in conflict.headers
        assert REPLAY_HEADER not in created.headers
        assert created.json()["threshold"] in {60, 61}

        async with sessionmaker() as session:
            alerts = await session.scalar(
                select(func.count())
                .select_from(Alert)
                .where(Alert.field_id == field_id, Alert.event_type == "hail")
            )
            keys = await session.scalar(
                select(func.count())
                .select_from(IdempotencyKey)
                .where(IdempotencyKey.scope == "POST /api/v1/alerts", IdempotencyKey.key == key)
            )
        assert alerts == 1
        assert keys == 1
    finally:
        async with sessionmaker() as session:
            await session.execute(delete(User).where(User.phone_number == phone_number))
            await session.execute(delete(IdempotencyKey).where(IdempotencyKey.key == key))
            await session.commit()
        await engine.dispose()


async def test_concurrent_post_users_with_the_same_key_create_one_user() -> None:
    """`POST /users` sin auth: `user_id NULL` + UNIQUE `NULLS NOT DISTINCT`, un solo usuario."""
    engine: AsyncEngine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    phone_number = f"+54{uuid4().int % 10**12:012d}"
    key = f"concurrent-users-{uuid4()}"
    try:
        body = {"phone_number": phone_number, "name": "QA Concurrent User"}
        async with (
            _client_with_committed_session(sessionmaker) as first,
            _client_with_committed_session(sessionmaker) as second,
        ):
            responses = await asyncio.gather(
                first.post(USERS, json=body, headers={KEY_HEADER: key}),
                second.post(USERS, json=body, headers={KEY_HEADER: key}),
            )

        assert {response.status_code for response in responses} == {201}
        assert responses[0].json() == responses[1].json()
        replayed = [response.headers.get(REPLAY_HEADER) == "true" for response in responses]
        assert replayed.count(True) == 1

        async with sessionmaker() as session:
            users = await session.scalar(
                select(func.count()).select_from(User).where(User.phone_number == phone_number)
            )
            keys = await session.scalar(
                select(func.count())
                .select_from(IdempotencyKey)
                .where(IdempotencyKey.scope.startswith("POST /api/v1/users"))
                .where(IdempotencyKey.key == key)
            )
            stored_user_id = (
                await session.execute(
                    select(IdempotencyKey.user_id).where(IdempotencyKey.key == key)
                )
            ).scalar_one()
        assert users == 1
        assert keys == 1
        assert stored_user_id is None
    finally:
        async with sessionmaker() as session:
            await session.execute(delete(User).where(User.phone_number == phone_number))
            await session.execute(delete(IdempotencyKey).where(IdempotencyKey.key == key))
            await session.commit()
        await engine.dispose()


# --- Sin header y endpoints sin idempotencia ----------------------------------


@pytest.mark.parametrize("case_name", ["fields", "alerts"])
async def test_without_the_header_an_identical_post_conflicts_as_before(
    client: AsyncClient, db_session: AsyncSession, cases: dict[str, Case], case_name: str
) -> None:
    """Sin header no hay deduplicación: el segundo POST idéntico sigue dando 409."""
    case = cases[case_name]

    first = await client.post(case.path, json=case.body, headers=case.headers)
    second = await client.post(case.path, json=case.body, headers=case.headers)

    assert first.status_code == 201
    assert second.status_code == 409
    assert REPLAY_HEADER not in second.headers
    stored = await db_session.scalar(select(func.count()).select_from(IdempotencyKey))
    assert stored == 0


async def test_the_key_is_ignored_by_endpoints_without_idempotency(
    client: AsyncClient,
    db_session: AsyncSession,
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    """Solo los tres POST soportan la clave; en un PATCH el header se ignora."""
    user = await make_user()
    field = await make_field(user=user)
    alert = await make_alert(field)

    response = await client.patch(
        f"{ALERTS}/{alert.id}",
        json={"threshold": 90},
        headers={**auth(user), KEY_HEADER: "patch-key"},
    )

    assert response.status_code == 200
    assert REPLAY_HEADER not in response.headers
    assert await _stored_key_count(db_session, "POST /api/v1/alerts", "patch-key") == 0


async def test_post_users_from_two_clients_replays_the_same_body_and_isolates_others(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """`POST /users` sin auth: misma clave + mismo body -> replay; otro body -> clave nueva.

    SEC-1: el alcance incluye el hash del body, así que dos clientes con la misma clave y
    bodies distintos se tratan como claves independientes (no hay 422 cruzado). La fila
    guardada queda con `user_id NULL` y sin PII (SEC-2).
    """
    body = {"phone_number": f"+54{uuid4().int % 10**12:012d}", "name": "QA Two Clients"}
    other_body = {"phone_number": f"+54{uuid4().int % 10**12:012d}", "name": "QA Two Clients"}
    app = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as second:
        first = await client.post(USERS, json=body, headers={KEY_HEADER: "two-clients"})
        replay = await second.post(USERS, json=body, headers={KEY_HEADER: "two-clients"})
        other = await second.post(
            USERS,
            json=other_body,
            headers={KEY_HEADER: "two-clients"},
        )

    assert first.status_code == 201
    assert replay.status_code == 201
    assert replay.headers[REPLAY_HEADER] == "true"
    assert replay.content == first.content
    assert other.status_code == 201
    assert REPLAY_HEADER not in other.headers
    assert other.json()["id"] != first.json()["id"]
    stored = (
        await db_session.execute(
            select(IdempotencyKey).where(
                IdempotencyKey.scope.startswith("POST /api/v1/users"),
                IdempotencyKey.key == "two-clients",
            )
        )
    ).scalars()
    rows = list(stored.all())
    assert len(rows) == 2
    assert all(row.user_id is None for row in rows)
    assert all(body["phone_number"] not in row.response_body for row in rows)


# --- Normalización y vencimiento ----------------------------------------------


async def test_replay_compares_the_normalized_body(client: AsyncClient) -> None:
    """El hash usa el body validado: espacios del nombre o formato del número no importan."""
    phone_number = f"+54{uuid4().int % 10**12:012d}"

    first = await client.post(
        USERS,
        json={"phone_number": phone_number, "name": "  Ana  "},
        headers={KEY_HEADER: "normalized-body-key"},
    )
    replay = await client.post(
        USERS,
        json={"phone_number": phone_number, "name": "Ana"},
        headers={KEY_HEADER: "normalized-body-key"},
    )

    assert first.status_code == 201
    assert first.json()["name"] == "Ana"
    assert replay.status_code == 201
    assert replay.headers[REPLAY_HEADER] == "true"
    assert replay.json() == first.json()


async def test_an_expired_key_with_a_different_body_is_treated_as_new(
    client: AsyncClient, db_session: AsyncSession, cases: dict[str, Case]
) -> None:
    """Vencida la clave no se compara el body: se descarta y se ejecuta como nueva."""
    case = cases["fields"]
    first = await _post(client, case, "expired-different-key")
    assert first.status_code == 201
    await db_session.execute(
        update(IdempotencyKey)
        .where(IdempotencyKey.scope == case.scope, IdempotencyKey.key == "expired-different-key")
        .values(expires_at=datetime(2020, 1, 1, tzinfo=UTC))
    )
    db_session.expire_all()

    second = await _post(client, case, "expired-different-key", body=case.fixed_body)

    assert second.status_code == 201
    assert REPLAY_HEADER not in second.headers
    assert second.json()["id"] != first.json()["id"]
    assert await _stored_key_count(db_session, case.scope, "expired-different-key") == 1


async def test_the_saved_response_expires_after_24_hours(
    client: AsyncClient, db_session: AsyncSession, cases: dict[str, Case]
) -> None:
    """`IDEMPOTENCY_TTL_HOURS` (default 24) se calcula con el reloj de la base."""
    case = cases["fields"]
    await _post(client, case, "ttl-key")

    stored = (
        await db_session.execute(select(IdempotencyKey).where(IdempotencyKey.key == "ttl-key"))
    ).scalar_one()

    assert stored.expires_at - stored.created_at == timedelta(hours=24)


# --- Migración 0004 -----------------------------------------------------------


def test_migration_0004_upgrade_and_downgrade_are_clean() -> None:
    """La migración `0004` se revierte y se vuelve a aplicar sin errores."""
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)

    command.downgrade(cfg, "0003")
    command.upgrade(cfg, "head")
