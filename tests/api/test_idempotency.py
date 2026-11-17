"""Tests de `Idempotency-Key` en `POST /alerts` (S10, S11b). Ver docs/API.md.

Cubre primera vez, replay byte a byte, body distinto, header ausente/inválido, operación
fallida, clave vencida, alcance por usuario, concurrencia real y atomicidad entre la alerta
y la clave guardada. También verifica que `POST /users` y `POST /fields` ya no la usan: el
header se ignora y sus duplicados los resuelve la unicidad natural (409).
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
from app.services import idempotency as idempotency_service
from app.services.idempotency import (
    INVALID_KEY_DETAIL,
    KEY_REUSED_DETAIL,
    MISSING_KEY_DETAIL,
    compute_request_hash,
)
from tests.conftest import (
    TEST_DATABASE_URL,
    ApiClient,
    MakeField,
    MakeUser,
    auth,
)

ROOT = Path(__file__).resolve().parents[2]
API = "/api/v1"
USERS = f"{API}/users"
FIELDS = f"{API}/fields"
ALERTS = f"{API}/alerts"
KEY_HEADER = "Idempotency-Key"
REPLAY_HEADER = "Idempotent-Replayed"


@dataclass(frozen=True)
class AlertCase:
    """`POST /alerts` con un body válido, otro body válido distinto y su dueño."""

    path: str
    scope: str
    headers: dict[str, str]
    body: dict[str, Any]
    fixed_body: dict[str, Any]
    user_id: int
    field_id: int


@pytest.fixture
async def case(make_user: MakeUser, make_field: MakeField) -> AlertCase:
    user = await make_user()
    field = await make_field(user=user)
    return AlertCase(
        path=ALERTS,
        scope="POST /api/v1/alerts",
        headers=auth(user),
        body={"field_id": field.id, "event_type": "frost", "threshold": 70},
        fixed_body={"field_id": field.id, "event_type": "frost", "threshold": 80},
        user_id=user.id,
        field_id=field.id,
    )


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

    Un 404/409 de la API hace `rollback` de su transacción; con `client`/`db_session` ese
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
    case: AlertCase,
    key: str,
    *,
    body: dict[str, Any] | None = None,
) -> Any:
    return await client.post(
        case.path,
        json=case.body if body is None else body,
        headers={**case.headers, KEY_HEADER: key},
    )


async def _stored_keys(session: AsyncSession, case: AlertCase, key: str) -> list[IdempotencyKey]:
    result = await session.execute(
        select(IdempotencyKey).where(
            IdempotencyKey.user_id == case.user_id,
            IdempotencyKey.scope == case.scope,
            IdempotencyKey.key == key,
        )
    )
    return list(result.scalars().all())


async def _resource_count(session: AsyncSession, case: AlertCase) -> int:
    query = (
        select(func.count())
        .select_from(Alert)
        .where(
            Alert.field_id == case.body["field_id"],
            Alert.event_type == case.body["event_type"],
            Alert.threshold == case.body["threshold"],
            Alert.deleted_at.is_(None),
        )
    )
    return (await session.execute(query)).scalar_one()


def _client_with_committed_session(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> ApiClient:
    """Cliente ASGI con una sesión por request contra datos ya commiteados."""
    app = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    app.dependency_overrides[get_session] = _override_session
    return ApiClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        auto_idempotency=False,
    )


async def _stored_key_count(session: AsyncSession, scope: str, key: str) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(IdempotencyKey)
            .where(IdempotencyKey.scope == scope, IdempotencyKey.key == key)
        )
    ).scalar_one()


# --- Header obligatorio -------------------------------------------------------


async def test_missing_header_returns_422_and_creates_nothing(
    raw_client: AsyncClient, db_session: AsyncSession, case: AlertCase
) -> None:
    response = await raw_client.post(case.path, json=case.body, headers=case.headers)

    assert response.status_code == 422
    assert response.json() == {"detail": MISSING_KEY_DETAIL}
    assert await _resource_count(db_session, case) == 0
    stored = await db_session.scalar(select(func.count()).select_from(IdempotencyKey))
    assert stored == 0


async def test_missing_header_does_not_create_anything_even_with_a_valid_body_again(
    raw_client: AsyncClient, db_session: AsyncSession, case: AlertCase
) -> None:
    """Dos intentos sin header: el contrato es el mismo y nunca se crea la alerta."""
    first = await raw_client.post(case.path, json=case.body, headers=case.headers)
    second = await raw_client.post(case.path, json=case.body, headers=case.headers)

    assert [first.status_code, second.status_code] == [422, 422]
    assert first.json() == second.json() == {"detail": MISSING_KEY_DETAIL}
    assert await _resource_count(db_session, case) == 0


# --- Primera vez y replay -----------------------------------------------------


async def test_first_post_with_a_key_creates_and_stores_the_response(
    client: AsyncClient, db_session: AsyncSession, case: AlertCase
) -> None:
    response = await _post(client, case, "first-key")

    assert response.status_code == 201
    assert REPLAY_HEADER not in response.headers
    assert await _resource_count(db_session, case) == 1
    stored = await _stored_keys(db_session, case, "first-key")
    assert len(stored) == 1
    assert stored[0].status_code == 201
    # CR-1: TEXT con los mismos bytes que la respuesta.
    assert stored[0].response_body == response.text
    assert json.loads(stored[0].response_body) == response.json()
    assert stored[0].request_hash == compute_request_hash(AlertCreate.model_validate(case.body))
    assert stored[0].scope == case.scope
    assert stored[0].user_id == case.user_id
    assert stored[0].expires_at > datetime.now(UTC)


async def test_replay_returns_the_saved_response_without_a_second_resource(
    client: AsyncClient, db_session: AsyncSession, case: AlertCase
) -> None:
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


async def test_replay_ignores_json_key_order_and_whitespace(
    client: AsyncClient, db_session: AsyncSession, case: AlertCase
) -> None:
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


async def test_the_same_key_with_a_different_body_returns_422(
    client: AsyncClient, db_session: AsyncSession, case: AlertCase
) -> None:
    assert (await _post(client, case, "reused-key")).status_code == 201

    conflict = await _post(client, case, "reused-key", body=case.fixed_body)

    assert conflict.status_code == 422
    assert conflict.json() == {"detail": KEY_REUSED_DETAIL}
    assert await _resource_count(db_session, case) == 1


# --- Clave inválida -----------------------------------------------------------


@pytest.mark.parametrize("key", ["", "x" * 256, "with space", "\x7f"])
async def test_invalid_keys_return_422_and_do_not_create_anything(
    client: AsyncClient, db_session: AsyncSession, case: AlertCase, key: str
) -> None:
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


@pytest.mark.parametrize("key", ["a", "~", "!", "x" * 255, "A~!" + "z" * 252])
async def test_printable_ascii_keys_at_the_limits_are_accepted_and_replayed(
    client: AsyncClient, db_session: AsyncSession, case: AlertCase, key: str
) -> None:
    """1 y 255 caracteres; `~` y `!` (ASCII imprimibles) son claves válidas."""
    first = await _post(client, case, key)

    assert first.status_code == 201
    assert REPLAY_HEADER not in first.headers
    stored = (await _stored_keys(db_session, case, key))[0]
    assert stored.key == key

    replay = await _post(client, case, key)

    assert replay.status_code == 201
    assert replay.headers[REPLAY_HEADER] == "true"
    assert replay.json() == first.json()
    assert await _resource_count(db_session, case) == 1


# --- Errores y vencimiento ----------------------------------------------------


async def test_a_failed_operation_does_not_store_the_key_and_the_retry_works(
    committed_env: CommittedEnv,
) -> None:
    env = committed_env
    key = "retry-key"
    failing = {"field_id": BIGINT_MAX, "event_type": "frost", "threshold": 70}
    fixed = {"field_id": env.field_id, "event_type": "frost", "threshold": 70}
    headers = {"X-User-Id": str(env.user_id), KEY_HEADER: key}
    try:
        failed = await env.client.post(ALERTS, json=failing, headers=headers)

        assert failed.status_code == 404
        async with env.sessionmaker() as session:
            stored = await session.scalar(
                select(func.count()).select_from(IdempotencyKey).where(IdempotencyKey.key == key)
            )
        assert stored == 0

        retried = await env.client.post(ALERTS, json=fixed, headers=headers)

        assert retried.status_code == 201
        assert REPLAY_HEADER not in retried.headers
        async with env.sessionmaker() as session:
            stored = await session.scalar(
                select(func.count()).select_from(IdempotencyKey).where(IdempotencyKey.key == key)
            )
        assert stored == 1
    finally:
        async with env.sessionmaker() as session:
            await session.execute(delete(IdempotencyKey).where(IdempotencyKey.key == key))
            await session.commit()


async def test_an_expired_key_is_treated_as_new(
    client: AsyncClient, db_session: AsyncSession, case: AlertCase
) -> None:
    first = await _post(client, case, "expired-key")
    assert first.status_code == 201
    await db_session.execute(
        update(IdempotencyKey)
        .where(IdempotencyKey.scope == case.scope, IdempotencyKey.key == "expired-key")
        .values(expires_at=datetime(2020, 1, 1, tzinfo=UTC))
    )
    db_session.expire_all()
    await db_session.execute(delete(Alert).where(Alert.id == first.json()["id"]))
    assert await _resource_count(db_session, case) == 0

    second = await _post(client, case, "expired-key")

    assert second.status_code == 201
    assert REPLAY_HEADER not in second.headers
    assert second.json()["id"] != first.json()["id"]
    assert await _resource_count(db_session, case) == 1


async def test_an_expired_key_with_a_different_body_is_treated_as_new(
    client: AsyncClient, db_session: AsyncSession, case: AlertCase
) -> None:
    """Vencida la clave no se compara el body: se descarta y se ejecuta como nueva."""
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
    client: AsyncClient, db_session: AsyncSession, case: AlertCase
) -> None:
    """`IDEMPOTENCY_TTL_HOURS` (default 24) se calcula con el reloj de la base."""
    await _post(client, case, "ttl-key")

    stored = (
        await db_session.execute(select(IdempotencyKey).where(IdempotencyKey.key == "ttl-key"))
    ).scalar_one()

    assert stored.expires_at - stored.created_at == timedelta(hours=24)


# --- Alcance de la clave ------------------------------------------------------


async def test_the_key_is_scoped_to_the_user(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField
) -> None:
    first_user = await make_user()
    second_user = await make_user()
    first_field = await make_field(user=first_user)
    second_field = await make_field(user=second_user)

    first = await client.post(
        ALERTS,
        json={"field_id": first_field.id, "event_type": "frost", "threshold": 70},
        headers={**auth(first_user), KEY_HEADER: "shared-key"},
    )
    second = await client.post(
        ALERTS,
        json={"field_id": second_field.id, "event_type": "frost", "threshold": 70},
        headers={**auth(second_user), KEY_HEADER: "shared-key"},
    )

    assert first.status_code == second.status_code == 201
    assert REPLAY_HEADER not in second.headers
    assert first.json()["id"] != second.json()["id"]

    replay = await client.post(
        ALERTS,
        json={"field_id": first_field.id, "event_type": "frost", "threshold": 70},
        headers={**auth(first_user), KEY_HEADER: "shared-key"},
    )
    assert replay.headers[REPLAY_HEADER] == "true"
    assert replay.json() == first.json()


async def test_a_repeated_header_uses_the_first_value(
    client: AsyncClient, db_session: AsyncSession, case: AlertCase
) -> None:
    """Header repetido: el contrato no lo define; se fija el comportamiento actual.

    Starlette/FastAPI toma el primer valor y descarta el resto, así que la clave guardada es
    la primera (docs/API.md §Idempotency-Key habla del header en singular).
    """
    response = await client.post(
        ALERTS,
        json=case.body,
        headers=[
            ("X-User-Id", str(case.user_id)),
            (KEY_HEADER, "first-key"),
            (KEY_HEADER, "second-key"),
        ],
    )

    assert response.status_code == 201
    assert REPLAY_HEADER not in response.headers
    stored = (
        await db_session.execute(
            select(IdempotencyKey.key).where(IdempotencyKey.user_id == case.user_id)
        )
    ).scalars()
    assert list(stored.all()) == ["first-key"]

    # El segundo valor se descartó: no hay clave guardada, así que el mismo body da 409.
    only_second = await client.post(
        ALERTS,
        json=case.body,
        headers={"X-User-Id": str(case.user_id), KEY_HEADER: "second-key"},
    )
    assert only_second.status_code == 409

    replay = await client.post(
        ALERTS,
        json=case.body,
        headers={"X-User-Id": str(case.user_id), KEY_HEADER: "first-key"},
    )
    assert replay.status_code == 201
    assert replay.headers[REPLAY_HEADER] == "true"


# --- Validación del body antes que la idempotencia ----------------------------


async def test_an_invalid_body_is_rejected_before_the_key_is_stored(
    client: AsyncClient,
    db_session: AsyncSession,
    case: AlertCase,
) -> None:
    """Un 422 de Pydantic no guarda la clave: el cliente corrige y reintenta con la misma."""
    key = "invalid-body-key"

    rejected = await client.post(
        ALERTS,
        json={"field_id": case.field_id, "event_type": "snow", "threshold": 70},
        headers={**case.headers, KEY_HEADER: key},
    )

    assert rejected.status_code == 422
    # 422 de Pydantic (lista de errores), no el de la idempotencia (detail plano).
    assert isinstance(rejected.json()["detail"], list)
    assert await _stored_key_count(db_session, case.scope, key) == 0

    accepted = await client.post(ALERTS, json=case.body, headers={**case.headers, KEY_HEADER: key})

    assert accepted.status_code == 201
    assert REPLAY_HEADER not in accepted.headers
    assert await _stored_key_count(db_session, case.scope, key) == 1


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
    case: AlertCase,
) -> None:
    """El replay devuelve el body guardado, no el estado actual del recurso."""
    first = await client.post(
        ALERTS, json=case.body, headers={**case.headers, KEY_HEADER: "changed-key"}
    )
    alert_id = first.json()["id"]
    patched = await client.patch(
        f"{ALERTS}/{alert_id}", json={"threshold": 80}, headers=case.headers
    )

    replay = await client.post(
        ALERTS, json=case.body, headers={**case.headers, KEY_HEADER: "changed-key"}
    )

    assert first.status_code == 201
    assert patched.status_code == 200
    assert patched.json()["threshold"] == 80
    assert replay.status_code == 201
    assert replay.headers[REPLAY_HEADER] == "true"
    assert replay.json() == first.json()
    assert replay.json()["threshold"] == 70

    current = await client.get(f"{ALERTS}/{alert_id}", headers=case.headers)
    assert current.json()["threshold"] == 80


async def test_replay_still_returns_the_saved_201_after_the_alert_is_soft_deleted(
    client: AsyncClient,
    db_session: AsyncSession,
    case: AlertCase,
) -> None:
    """El DELETE lógico del recurso no invalida la respuesta guardada."""
    first = await client.post(
        ALERTS, json=case.body, headers={**case.headers, KEY_HEADER: "deleted-key"}
    )
    alert_id = first.json()["id"]
    deleted = await client.delete(f"{ALERTS}/{alert_id}", headers=case.headers)

    replay = await client.post(
        ALERTS, json=case.body, headers={**case.headers, KEY_HEADER: "deleted-key"}
    )

    assert deleted.status_code == 204
    assert (await client.get(f"{ALERTS}/{alert_id}", headers=case.headers)).status_code == 404
    assert replay.status_code == 201
    assert replay.headers[REPLAY_HEADER] == "true"
    assert replay.json() == first.json()
    deleted_at = (
        await db_session.execute(select(Alert.deleted_at).where(Alert.id == alert_id))
    ).scalar_one()
    assert deleted_at is not None

    recreated = await client.post(
        ALERTS, json=case.body, headers={**case.headers, KEY_HEADER: "deleted-key-new"}
    )
    replayed_again = await client.post(
        ALERTS, json=case.body, headers={**case.headers, KEY_HEADER: "deleted-key"}
    )

    assert recreated.status_code == 201
    assert REPLAY_HEADER not in recreated.headers
    assert recreated.json()["id"] != alert_id
    assert replayed_again.json()["id"] == alert_id
    alive = (
        await db_session.execute(
            select(func.count())
            .select_from(Alert)
            .where(Alert.field_id == case.field_id, Alert.deleted_at.is_(None))
        )
    ).scalar_one()
    assert alive == 1


# --- Cascada al borrar el usuario ---------------------------------------------


async def test_deleting_the_user_cascades_his_keys() -> None:
    """FK `idempotency_keys.user_id ON DELETE CASCADE` (DATA_MODEL §idempotency_keys)."""
    engine: AsyncEngine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    phone_number = f"+54{uuid4().int % 10**12:012d}"
    key = f"cascade-{uuid4()}"
    try:
        async with sessionmaker() as session:
            user = User(phone_number=phone_number, name="QA Cascade")
            session.add(user)
            await session.flush()
            field = Field(
                user_id=user.id,
                name="Cascade Field",
                latitude=Decimal("-34.6"),
                longitude=Decimal("-58.4"),
            )
            session.add(field)
            await session.commit()
            user_id = user.id
            field_id = field.id

        async with _client_with_committed_session(sessionmaker) as client:
            created = await client.post(
                ALERTS,
                json={"field_id": field_id, "event_type": "frost", "threshold": 70},
                headers={"X-User-Id": str(user_id), KEY_HEADER: key},
            )

        assert created.status_code == 201
        async with sessionmaker() as session:
            stored = await session.scalar(
                select(func.count())
                .select_from(IdempotencyKey)
                .where(IdempotencyKey.user_id == user_id)
            )
        assert stored == 1

        async with sessionmaker() as session:
            await session.execute(delete(User).where(User.id == user_id))
            await session.commit()
            stored = await session.scalar(
                select(func.count())
                .select_from(IdempotencyKey)
                .where(IdempotencyKey.user_id == user_id)
            )
        assert stored == 0
    finally:
        async with sessionmaker() as session:
            await session.execute(delete(User).where(User.phone_number == phone_number))
            await session.execute(delete(IdempotencyKey).where(IdempotencyKey.key == key))
            await session.commit()
        await engine.dispose()


# --- Concurrencia y atomicidad ------------------------------------------------


async def test_concurrent_posts_with_the_same_key_create_exactly_one_resource() -> None:
    """Dos POST simultáneos con la misma clave: un solo recurso y un solo replay."""
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    phone_number = f"+54{uuid4().int % 10**12:012d}"
    key = f"concurrent-{uuid4()}"
    try:
        async with sessionmaker() as session:
            user = User(phone_number=phone_number, name="QA Idempotency")
            session.add(user)
            await session.flush()
            field = Field(
                user_id=user.id,
                name="Concurrent Field",
                latitude=Decimal("-34.6"),
                longitude=Decimal("-58.4"),
            )
            session.add(field)
            await session.commit()
            user_id = user.id
            field_id = field.id

        payload = {"field_id": field_id, "event_type": "frost", "threshold": 70}
        headers = {"X-User-Id": str(user_id), KEY_HEADER: key}
        async with (
            _client_with_committed_session(sessionmaker) as first,
            _client_with_committed_session(sessionmaker) as second,
        ):
            first_response, second_response = await asyncio.gather(
                first.post(ALERTS, json=payload, headers=headers),
                second.post(ALERTS, json=payload, headers=headers),
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
                select(func.count()).select_from(Alert).where(Alert.field_id == field_id)
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


async def test_a_failed_key_insert_rolls_back_the_resource(
    committed_env: CommittedEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = committed_env
    key = "atomic-key"
    body = {"field_id": env.field_id, "event_type": "frost", "threshold": 70}
    headers = {"X-User-Id": str(env.user_id), KEY_HEADER: key}

    async def _boom(*_: object, **__: object) -> None:
        raise RuntimeError("idempotency store failed")

    try:
        monkeypatch.setattr(idempotency_service, "_store", _boom)
        with pytest.raises(RuntimeError, match="idempotency store failed"):
            await env.client.post(ALERTS, json=body, headers=headers)

        # La alerta y la clave se guardan en la misma transacción: si la clave no se pudo
        # guardar, la alerta tampoco quedó (rollback).
        async with env.sessionmaker() as session:
            alerts = await session.scalar(
                select(func.count())
                .select_from(Alert)
                .where(Alert.field_id == env.field_id, Alert.deleted_at.is_(None))
            )
            keys = await session.scalar(
                select(func.count()).select_from(IdempotencyKey).where(IdempotencyKey.key == key)
            )
        assert alerts == 0
        assert keys == 0

        monkeypatch.undo()
        retried = await env.client.post(ALERTS, json=body, headers=headers)

        assert retried.status_code == 201
        async with env.sessionmaker() as session:
            alerts = await session.scalar(
                select(func.count())
                .select_from(Alert)
                .where(Alert.field_id == env.field_id, Alert.deleted_at.is_(None))
            )
        assert alerts == 1
    finally:
        async with env.sessionmaker() as session:
            await session.execute(delete(IdempotencyKey).where(IdempotencyKey.key == key))
            await session.commit()


# --- Endpoints sin idempotencia ------------------------------------------------


async def test_the_key_is_ignored_by_endpoints_without_idempotency(
    client: AsyncClient,
    db_session: AsyncSession,
    make_user: MakeUser,
    make_field: MakeField,
) -> None:
    """Solo `POST /alerts` soporta la clave; en un PATCH el header se ignora."""
    user = await make_user()
    field = await make_field(user=user)
    created = await client.post(
        ALERTS,
        json={"field_id": field.id, "event_type": "frost", "threshold": 70},
        headers=auth(user),
    )
    alert_id = created.json()["id"]

    response = await client.patch(
        f"{ALERTS}/{alert_id}",
        json={"threshold": 90},
        headers={**auth(user), KEY_HEADER: "patch-key"},
    )

    assert response.status_code == 200
    assert REPLAY_HEADER not in response.headers
    assert await _stored_key_count(db_session, "POST /api/v1/alerts", "patch-key") == 0


async def test_post_users_ignores_the_header(client: AsyncClient, db_session: AsyncSession) -> None:
    """`POST /users` ya no usa la clave: dos bodies distintos con la misma clave -> 201 y 201."""
    key = "users-ignored-key"
    first_body = {"phone_number": f"+54{uuid4().int % 10**12:012d}", "name": "Client A"}
    second_body = {"phone_number": f"+54{uuid4().int % 10**12:012d}", "name": "Client B"}

    first = await client.post(USERS, json=first_body, headers={KEY_HEADER: key})
    second = await client.post(USERS, json=second_body, headers={KEY_HEADER: key})

    assert first.status_code == second.status_code == 201
    assert REPLAY_HEADER not in first.headers
    assert REPLAY_HEADER not in second.headers
    assert first.json()["id"] != second.json()["id"]

    # El comportamiento normal de unicidad no cambió: mismo body -> 409.
    duplicate = await client.post(USERS, json=first_body, headers={KEY_HEADER: key})
    assert duplicate.status_code == 409
    assert REPLAY_HEADER not in duplicate.headers

    stored = await db_session.scalar(select(func.count()).select_from(IdempotencyKey))
    assert stored == 0


async def test_post_fields_ignores_the_header(
    client: AsyncClient, db_session: AsyncSession, make_user: MakeUser
) -> None:
    """`POST /fields`: misma clave y distinto body -> 201 y 201; nada se guarda."""
    user = await make_user()
    key = "fields-ignored-key"
    first_body = {"name": "Ignored A", "latitude": -34.6, "longitude": -58.4}
    second_body = {"name": "Ignored B", "latitude": -34.6, "longitude": -58.4}

    first = await client.post(FIELDS, json=first_body, headers={**auth(user), KEY_HEADER: key})
    second = await client.post(FIELDS, json=second_body, headers={**auth(user), KEY_HEADER: key})

    assert first.status_code == second.status_code == 201
    assert REPLAY_HEADER not in first.headers
    assert REPLAY_HEADER not in second.headers
    assert first.json()["id"] != second.json()["id"]

    # El duplicado lo resuelve la unicidad natural (nombre por usuario) -> 409.
    duplicate = await client.post(FIELDS, json=first_body, headers={**auth(user), KEY_HEADER: key})
    assert duplicate.status_code == 409
    assert REPLAY_HEADER not in duplicate.headers

    stored = await db_session.scalar(select(func.count()).select_from(IdempotencyKey))
    assert stored == 0


# --- Migración 0004 -----------------------------------------------------------


def test_migration_0004_upgrade_and_downgrade_are_clean() -> None:
    """La migración `0004` se revierte y se vuelve a aplicar sin errores."""
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)

    command.downgrade(cfg, "0003")
    command.upgrade(cfg, "head")
