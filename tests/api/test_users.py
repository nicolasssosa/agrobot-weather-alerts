"""Tests de `/users`. Ver docs/TESTING.md §test_users y docs/API.md §users."""

import pytest
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import _parse_user_id
from app.schemas.common import BIGINT_MAX
from app.schemas.user import UserCreate
from app.services.users import create_user
from tests.conftest import MakeUser, auth

API = "/api/v1/users"


async def test_create_user_returns_201_and_the_public_fields(client: AsyncClient) -> None:
    response = await client.post(
        API, json={"phone_number": "+5491122334455", "name": "  Juan Pérez  "}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["id"] > 0
    assert body["phone_number"] == "+5491122334455"
    assert body["name"] == "Juan Pérez"
    assert body["created_at"]


async def test_duplicate_phone_number_returns_409(client: AsyncClient) -> None:
    payload = {"phone_number": "+5491122334455", "name": "Ana"}
    assert (await client.post(API, json=payload)).status_code == 201

    response = await client.post(API, json={**payload, "name": "Otra Ana"})

    assert response.status_code == 409
    assert response.json() == {"detail": "User with this phone number already exists"}


@pytest.mark.parametrize(
    "phone_number",
    [
        "5491122334455",
        "+0123456789",
        "+1234567",
        "+1234567890123456",
        "abc",
        "",
    ],
)
async def test_invalid_phone_number_returns_422(client: AsyncClient, phone_number: str) -> None:
    response = await client.post(API, json={"phone_number": phone_number, "name": "Ana"})

    assert response.status_code == 422


@pytest.mark.parametrize("name", ["", "   ", "x" * 121])
async def test_invalid_name_returns_422(client: AsyncClient, name: str) -> None:
    response = await client.post(API, json={"phone_number": "+5491122334455", "name": name})

    assert response.status_code == 422


async def test_unknown_field_returns_422(client: AsyncClient) -> None:
    response = await client.post(
        API,
        json={"phone_number": "+5491122334455", "name": "Ana", "is_admin": True},
    )

    assert response.status_code == 422


async def test_current_user_without_header_returns_401(client: AsyncClient) -> None:
    response = await client.get(f"{API}/me")

    assert response.status_code == 401
    assert response.json() == {"detail": "Missing or invalid X-User-Id header"}


@pytest.mark.parametrize("header", ["abc", "1.5", ""])
async def test_current_user_with_non_numeric_header_returns_401(
    client: AsyncClient, header: str
) -> None:
    response = await client.get(f"{API}/me", headers={"X-User-Id": header})

    assert response.status_code == 401
    assert response.json() == {"detail": "Missing or invalid X-User-Id header"}


async def test_current_user_with_unknown_id_returns_401(client: AsyncClient) -> None:
    response = await client.get(f"{API}/me", headers={"X-User-Id": "999999999"})

    assert response.status_code == 401
    assert response.json() == {"detail": "Missing or invalid X-User-Id header"}


async def test_current_user_returns_the_authenticated_user(
    client: AsyncClient, make_user: MakeUser
) -> None:
    user = await make_user(name="Ana")

    response = await client.get(f"{API}/me", headers=auth(user))

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == user.id
    assert body["phone_number"] == user.phone_number
    assert body["name"] == "Ana"
    assert body["created_at"]


async def test_create_user_does_not_require_the_auth_header(client: AsyncClient) -> None:
    response = await client.post(API, json={"phone_number": "+5491122334456", "name": "Sin Header"})

    assert response.status_code == 201


async def test_create_user_with_a_120_character_name_is_accepted(client: AsyncClient) -> None:
    name = "x" * 120

    response = await client.post(API, json={"phone_number": "+5491122334457", "name": name})

    assert response.status_code == 201
    assert response.json()["name"] == name


async def test_user_read_exposes_only_the_documented_fields(client: AsyncClient) -> None:
    response = await client.post(API, json={"phone_number": "+5491122334458", "name": "Ana"})

    assert response.status_code == 201
    assert set(response.json()) == {"id", "phone_number", "name", "created_at"}


@pytest.mark.parametrize(
    "phone_number",
    [" +5491122334455", "+549 1122334455", "+5491122334455 "],
)
async def test_phone_number_with_spaces_returns_422(client: AsyncClient, phone_number: str) -> None:
    response = await client.post(API, json={"phone_number": phone_number, "name": "Ana"})

    assert response.status_code == 422


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded"])
async def test_create_user_with_an_incorrect_content_type_returns_422(
    client: AsyncClient, content_type: str
) -> None:
    response = await client.post(
        API,
        content='{"phone_number": "+5491122334455", "name": "Ana"}',
        headers={"content-type": content_type},
    )

    assert response.status_code == 422


@pytest.mark.parametrize("header", ["-1", "-999999999", "0"])
async def test_current_user_with_a_negative_or_zero_id_returns_401(
    client: AsyncClient, header: str
) -> None:
    response = await client.get(f"{API}/me", headers={"X-User-Id": header})

    assert response.status_code == 401
    assert response.json() == {"detail": "Missing or invalid X-User-Id header"}


@pytest.mark.parametrize("header", [str(BIGINT_MAX + 1), "99999999999999999999", "9" * 5000])
async def test_current_user_with_an_out_of_range_id_returns_401(
    client: AsyncClient, header: str
) -> None:
    response = await client.get(f"{API}/me", headers={"X-User-Id": header})

    assert response.status_code == 401
    assert response.json() == {"detail": "Missing or invalid X-User-Id header"}


@pytest.mark.parametrize("header", ["+1", " 1 "])
async def test_current_user_with_a_non_ascii_numeric_header_returns_401(
    client: AsyncClient, header: str
) -> None:
    response = await client.get(f"{API}/me", headers={"X-User-Id": header})

    assert response.status_code == 401
    assert response.json() == {"detail": "Missing or invalid X-User-Id header"}


@pytest.mark.parametrize("header", ["\u0661", "\uff11", "+1", " 1 ", "1.5", ""])
def test_user_id_header_only_accepts_ascii_digits(header: str) -> None:
    # Un header HTTP no puede transportar "\u0661" (Starlette lo decodifica latin-1), así que
    # el caso de dígitos Unicode se cubre directo sobre el parser.
    assert _parse_user_id(header) is None


def test_user_id_header_parses_ascii_digits() -> None:
    assert _parse_user_id("0001") == 1
    assert _parse_user_id(str(BIGINT_MAX)) == BIGINT_MAX


async def test_phone_number_with_unicode_digits_returns_422(client: AsyncClient) -> None:
    phone_number = "+9" + "\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668"
    response = await client.post(API, json={"phone_number": phone_number, "name": "Ana"})

    assert response.status_code == 422


@pytest.mark.parametrize("name", ["a\u0000b", "\u0000", "a\u0007b"])
async def test_name_with_control_characters_returns_422(client: AsyncClient, name: str) -> None:
    response = await client.post(API, json={"phone_number": "+5491122334460", "name": name})

    assert response.status_code == 422


async def test_non_unique_integrity_error_is_not_mapped_to_409(
    db_session: AsyncSession,
) -> None:
    # `model_construct` esquiva Pydantic para forzar una violación del CHECK de E.164
    # (sqlstate 23514): no es un duplicado, así que no debe convertirse en `ConflictError`.
    payload = UserCreate.model_construct(phone_number="123", name="Ana")

    with pytest.raises(IntegrityError):
        await create_user(db_session, payload)
