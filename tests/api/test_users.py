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
    assert set(response.json()) == {
        "id",
        "phone_number",
        "name",
        "timezone",
        "quiet_hours",
        "created_at",
    }


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


# --- Zona horaria y horario de silencio (S13, D15) --------------------------


async def test_create_user_defaults_to_the_buenos_aires_timezone_without_quiet_hours(
    client: AsyncClient,
) -> None:
    response = await client.post(API, json={"phone_number": "+5491122334470", "name": "Ana"})

    assert response.status_code == 201
    assert response.json()["timezone"] == "America/Argentina/Buenos_Aires"
    assert response.json()["quiet_hours"] is None


async def test_create_user_accepts_a_timezone_and_a_quiet_hours_window(
    client: AsyncClient,
) -> None:
    response = await client.post(
        API,
        json={
            "phone_number": "+5491122334471",
            "name": "Ana",
            "timezone": "America/Argentina/Cordoba",
            "quiet_hours": {"start": "22:00", "end": "07:00"},
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["timezone"] == "America/Argentina/Cordoba"
    assert body["quiet_hours"] == {"start": "22:00", "end": "07:00"}


@pytest.mark.parametrize(
    "quiet_hours",
    [
        {"start": "22:00", "end": "22:00"},
        {"start": "22:00:30", "end": "07:00"},
        {"start": "22:00+03:00", "end": "07:00"},
        {"start": "25:00", "end": "07:00"},
        {"start": "22:00"},
    ],
)
async def test_create_user_with_an_invalid_quiet_hours_window_returns_422(
    client: AsyncClient, quiet_hours: dict[str, str]
) -> None:
    response = await client.post(
        API,
        json={
            "phone_number": "+5491122334472",
            "name": "Ana",
            "quiet_hours": quiet_hours,
        },
    )

    assert response.status_code == 422


@pytest.mark.parametrize("timezone", ["Mars/Olympus", "localtime", "", "../etc/passwd"])
async def test_create_user_with_an_invalid_timezone_returns_422(
    client: AsyncClient, timezone: str
) -> None:
    response = await client.post(
        API,
        json={"phone_number": "+5491122334473", "name": "Ana", "timezone": timezone},
    )

    assert response.status_code == 422


async def test_patch_sets_the_quiet_hours_window(client: AsyncClient, make_user: MakeUser) -> None:
    user = await make_user()

    response = await client.patch(
        f"{API}/me", json={"quiet_hours": {"start": "22:00", "end": "07:00"}}, headers=auth(user)
    )

    assert response.status_code == 200
    assert response.json()["quiet_hours"] == {"start": "22:00", "end": "07:00"}
    # La zona no se toca si no se envía.
    assert response.json()["timezone"] == "America/Argentina/Buenos_Aires"


async def test_patch_updates_the_timezone(client: AsyncClient, make_user: MakeUser) -> None:
    user = await make_user()

    response = await client.patch(
        f"{API}/me",
        json={"timezone": "America/Argentina/Cordoba"},
        headers=auth(user),
    )

    assert response.status_code == 200
    assert response.json()["timezone"] == "America/Argentina/Cordoba"


async def test_patch_null_quiet_hours_disables_the_silence(
    client: AsyncClient, make_user: MakeUser
) -> None:
    user = await make_user()
    await client.patch(
        f"{API}/me", json={"quiet_hours": {"start": "22:00", "end": "07:00"}}, headers=auth(user)
    )

    response = await client.patch(f"{API}/me", json={"quiet_hours": None}, headers=auth(user))

    assert response.status_code == 200
    assert response.json()["quiet_hours"] is None


async def test_patch_omitting_quiet_hours_keeps_the_configured_window(
    client: AsyncClient, make_user: MakeUser
) -> None:
    user = await make_user()
    await client.patch(
        f"{API}/me", json={"quiet_hours": {"start": "22:00", "end": "07:00"}}, headers=auth(user)
    )

    response = await client.patch(
        f"{API}/me", json={"timezone": "America/Argentina/Cordoba"}, headers=auth(user)
    )

    assert response.status_code == 200
    assert response.json()["quiet_hours"] == {"start": "22:00", "end": "07:00"}
    assert response.json()["timezone"] == "America/Argentina/Cordoba"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"quiet_hours": {"start": "22:00", "end": "22:00"}},
        {"quiet_hours": {"start": "22:00:30", "end": "07:00"}},
        {"quiet_hours": {"start": "22:00+03:00", "end": "07:00"}},
        {"timezone": "Mars/Olympus"},
        {"timezone": "localtime"},
        {"timezone": None},
        {"phone_number": "+5491122334499"},
        {"name": "Otro Nombre"},
        {"quiet_hours": {"start": "22:00", "end": "07:00", "extra": 1}},
    ],
)
async def test_invalid_patch_returns_422(
    client: AsyncClient, make_user: MakeUser, payload: dict[str, object]
) -> None:
    user = await make_user()

    response = await client.patch(f"{API}/me", json=payload, headers=auth(user))

    assert response.status_code == 422


async def test_patch_without_the_auth_header_returns_401(client: AsyncClient) -> None:
    response = await client.patch(f"{API}/me", json={"quiet_hours": None})

    assert response.status_code == 401
    assert response.json() == {"detail": "Missing or invalid X-User-Id header"}


async def test_patch_only_touches_the_authenticated_user(
    client: AsyncClient, make_user: MakeUser
) -> None:
    ana = await make_user()
    bruno = await make_user()

    await client.patch(
        f"{API}/me", json={"quiet_hours": {"start": "22:00", "end": "07:00"}}, headers=auth(ana)
    )

    other = await client.get(f"{API}/me", headers=auth(bruno))
    assert other.json()["quiet_hours"] is None
    assert other.json()["timezone"] == "America/Argentina/Buenos_Aires"


@pytest.mark.parametrize("invalid_time", ["24:00", "7:00", "07:00Z"])
async def test_quiet_hours_outside_hh_mm_returns_422(
    client: AsyncClient, make_user: MakeUser, invalid_time: str
) -> None:
    """API.md §users: horas locales `HH:MM` (00:00-23:59), sin segundos ni offset."""
    user = await make_user()

    response = await client.patch(
        f"{API}/me",
        json={"quiet_hours": {"start": invalid_time, "end": "23:00"}},
        headers=auth(user),
    )

    assert response.status_code == 422


async def test_a_timezone_that_postgres_does_not_know_exactly_returns_422(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """La fuente de verdad es `pg_timezone_names` (API.md §users), no `zoneinfo`.

    En macOS `zoneinfo` resuelve la variante en minúsculas (filesystem case-insensitive) y la
    rechaza el service; en otros SO la rechaza el schema. En ambos casos es 422 y no se
    escribe nada.
    """
    user = await make_user()

    created = await client.post(
        API,
        json={
            "phone_number": "+5491122334480",
            "name": "Ana",
            "timezone": "america/argentina/buenos_aires",
        },
    )
    patched = await client.patch(
        f"{API}/me",
        json={"timezone": "america/argentina/buenos_aires"},
        headers=auth(user),
    )

    assert created.status_code == 422
    assert patched.status_code == 422
    me = await client.get(f"{API}/me", headers=auth(user))
    assert me.json()["timezone"] == "America/Argentina/Buenos_Aires"
