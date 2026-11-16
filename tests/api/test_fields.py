"""Tests de `/fields`. Ver docs/TESTING.md §test_fields y docs/API.md §fields."""

import pytest
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.common import BIGINT_MAX
from app.schemas.field import FieldCreate
from app.services.fields import create_field
from tests.conftest import MakeField, MakeUser, auth

API = "/api/v1/fields"


async def test_create_field_returns_201_and_appears_in_the_listing(
    client: AsyncClient, make_user: MakeUser
) -> None:
    user = await make_user()

    response = await client.post(
        API,
        json={"name": "  Lote Norte  ", "latitude": -34.6037, "longitude": -58.3816},
        headers=auth(user),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["id"] > 0
    assert body["name"] == "Lote Norte"
    assert body["latitude"] == pytest.approx(-34.6037)
    assert body["longitude"] == pytest.approx(-58.3816)

    listing = await client.get(API, headers=auth(user))
    assert listing.status_code == 200
    assert listing.json()["items"] == [body]


async def test_duplicate_field_name_for_the_same_user_returns_409(
    client: AsyncClient, make_user: MakeUser
) -> None:
    user = await make_user()
    await client.post(
        API,
        json={"name": "El Lote", "latitude": -34.6, "longitude": -58.4},
        headers=auth(user),
    )

    response = await client.post(
        API,
        json={"name": "El Lote", "latitude": -34.6, "longitude": -58.4},
        headers=auth(user),
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Field name already exists"}


async def test_same_field_name_for_another_user_returns_201(
    client: AsyncClient, make_user: MakeUser
) -> None:
    first = await make_user()
    second = await make_user()
    payload = {"name": "El Lote", "latitude": -34.6, "longitude": -58.4}
    assert (await client.post(API, json=payload, headers=auth(first))).status_code == 201

    response = await client.post(API, json=payload, headers=auth(second))

    assert response.status_code == 201


@pytest.mark.parametrize(
    ("name", "latitude", "longitude"),
    [
        ("Lote", 91, -58.4),
        ("Lote", -91, -58.4),
        ("Lote", -34.6, 181),
        ("Lote", -34.6, -181),
        ("", -34.6, -58.4),
        ("   ", -34.6, -58.4),
        ("x" * 121, -34.6, -58.4),
    ],
)
async def test_invalid_field_payload_returns_422(
    client: AsyncClient, make_user: MakeUser, name: str, latitude: float, longitude: float
) -> None:
    user = await make_user()

    response = await client.post(
        API,
        json={"name": name, "latitude": latitude, "longitude": longitude},
        headers=auth(user),
    )

    assert response.status_code == 422


async def test_unknown_field_in_payload_returns_422(
    client: AsyncClient, make_user: MakeUser
) -> None:
    user = await make_user()

    response = await client.post(
        API,
        json={"name": "Lote", "latitude": -34.6, "longitude": -58.4, "extra": 1},
        headers=auth(user),
    )

    assert response.status_code == 422


async def test_reading_a_field_of_another_user_returns_404(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField
) -> None:
    owner = await make_user()
    intruder = await make_user()
    field = await make_field(user=owner)

    own = await client.get(f"{API}/{field.id}", headers=auth(owner))
    other = await client.get(f"{API}/{field.id}", headers=auth(intruder))

    assert own.status_code == 200
    assert other.status_code == 404
    assert other.json() == {"detail": "Field not found"}


async def test_reading_a_missing_field_returns_404(
    client: AsyncClient, make_user: MakeUser
) -> None:
    user = await make_user()

    response = await client.get(f"{API}/999999999", headers=auth(user))

    assert response.status_code == 404
    assert response.json() == {"detail": "Field not found"}


async def test_reading_own_field_returns_200(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField
) -> None:
    user = await make_user()
    field = await make_field(user=user, name="Lote Sur")

    response = await client.get(f"{API}/{field.id}", headers=auth(user))

    assert response.status_code == 200
    assert response.json()["name"] == "Lote Sur"


async def test_listing_is_paginated_and_ordered_by_id(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField
) -> None:
    user = await make_user()
    fields = [await make_field(user=user) for _ in range(3)]

    response = await client.get(f"{API}?limit=2&offset=1", headers=auth(user))

    assert response.status_code == 200
    body = response.json()
    assert body["limit"] == 2
    assert body["offset"] == 1
    assert [item["id"] for item in body["items"]] == [fields[1].id, fields[2].id]


async def test_listing_defaults_to_limit_50_offset_0(
    client: AsyncClient, make_user: MakeUser
) -> None:
    user = await make_user()

    response = await client.get(API, headers=auth(user))

    assert response.status_code == 200
    assert response.json() == {"items": [], "limit": 50, "offset": 0}


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1", "limit=abc"])
async def test_invalid_pagination_returns_422(
    client: AsyncClient, make_user: MakeUser, query: str
) -> None:
    user = await make_user()

    response = await client.get(f"{API}?{query}", headers=auth(user))

    assert response.status_code == 422


async def test_listing_without_header_returns_401(client: AsyncClient) -> None:
    response = await client.get(API)

    assert response.status_code == 401


async def test_create_field_without_header_returns_401(client: AsyncClient) -> None:
    response = await client.post(API, json={"name": "Lote", "latitude": -34.6, "longitude": -58.4})

    assert response.status_code == 401
    assert response.json() == {"detail": "Missing or invalid X-User-Id header"}


async def test_reading_a_field_without_header_returns_401(client: AsyncClient) -> None:
    response = await client.get(f"{API}/1")

    assert response.status_code == 401
    assert response.json() == {"detail": "Missing or invalid X-User-Id header"}


async def test_listing_returns_only_the_authenticated_users_fields(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField
) -> None:
    owner = await make_user()
    other = await make_user()
    own_fields = [await make_field(user=owner) for _ in range(2)]
    await make_field(user=other, name="Ajeno")

    response = await client.get(API, headers=auth(owner))

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"items", "limit", "offset"}
    assert [item["id"] for item in body["items"]] == [field.id for field in own_fields]


async def test_listing_accepts_the_maximum_limit(client: AsyncClient, make_user: MakeUser) -> None:
    user = await make_user()

    response = await client.get(f"{API}?limit=100", headers=auth(user))

    assert response.status_code == 200
    assert response.json() == {"items": [], "limit": 100, "offset": 0}


async def test_listing_with_an_offset_beyond_the_total_returns_empty_items(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField
) -> None:
    user = await make_user()
    await make_field(user=user)
    await make_field(user=user)

    response = await client.get(f"{API}?offset=10", headers=auth(user))

    assert response.status_code == 200
    assert response.json() == {"items": [], "limit": 50, "offset": 10}


async def test_field_read_exposes_only_the_documented_fields(
    client: AsyncClient, make_user: MakeUser
) -> None:
    user = await make_user()

    response = await client.post(
        API,
        json={"name": "Lote", "latitude": -34.6, "longitude": -58.4},
        headers=auth(user),
    )

    assert response.status_code == 201
    assert set(response.json()) == {"id", "name", "latitude", "longitude", "created_at"}


async def test_create_field_with_a_120_character_name_is_accepted(
    client: AsyncClient, make_user: MakeUser
) -> None:
    user = await make_user()
    name = "x" * 120

    response = await client.post(
        API,
        json={"name": name, "latitude": -34.6, "longitude": -58.4},
        headers=auth(user),
    )

    assert response.status_code == 201
    assert response.json()["name"] == name


@pytest.mark.parametrize(
    ("latitude", "longitude"),
    [(90, 180), (-90, -180), (90, -180), (-90, 180), (0, 0)],
)
async def test_coordinate_boundaries_are_accepted(
    client: AsyncClient,
    make_user: MakeUser,
    latitude: float,
    longitude: float,
) -> None:
    user = await make_user()

    response = await client.post(
        API,
        json={"name": "Lote", "latitude": latitude, "longitude": longitude},
        headers=auth(user),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["latitude"] == pytest.approx(latitude)
    assert body["longitude"] == pytest.approx(longitude)


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded"])
async def test_create_field_with_an_incorrect_content_type_returns_422(
    client: AsyncClient, make_user: MakeUser, content_type: str
) -> None:
    user = await make_user()

    response = await client.post(
        API,
        content='{"name": "Lote", "latitude": -34.6, "longitude": -58.4}',
        headers={**auth(user), "content-type": content_type},
    )

    assert response.status_code == 422


@pytest.mark.parametrize("field_id", [str(BIGINT_MAX + 1), "99999999999999999999"])
async def test_reading_a_field_with_an_out_of_range_id_returns_404(
    client: AsyncClient, make_user: MakeUser, field_id: str
) -> None:
    user = await make_user()

    response = await client.get(f"{API}/{field_id}", headers=auth(user))

    assert response.status_code == 404
    assert response.json() == {"detail": "Field not found"}


@pytest.mark.parametrize("offset", [str(BIGINT_MAX + 1), "99999999999999999999"])
async def test_listing_with_an_out_of_range_offset_returns_422(
    client: AsyncClient, make_user: MakeUser, offset: str
) -> None:
    user = await make_user()

    response = await client.get(f"{API}?offset={offset}", headers=auth(user))

    assert response.status_code == 422


@pytest.mark.parametrize("name", ["a\u0000b", "\u0000", "a\u0007b"])
async def test_name_with_control_characters_returns_422(
    client: AsyncClient, make_user: MakeUser, name: str
) -> None:
    user = await make_user()

    response = await client.post(
        API,
        json={"name": name, "latitude": -34.6, "longitude": -58.4},
        headers=auth(user),
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        '{"name": "n", "latitude": NaN, "longitude": 1}',
        '{"name": "n", "latitude": 1, "longitude": Infinity}',
        '{"name": "n", "latitude": -Infinity, "longitude": 1}',
    ],
)
async def test_non_finite_coordinates_return_a_serializable_422(
    client: AsyncClient, make_user: MakeUser, body: str
) -> None:
    user = await make_user()

    response = await client.post(
        API, content=body, headers={**auth(user), "content-type": "application/json"}
    )

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)


async def test_non_unique_integrity_error_is_not_mapped_to_409(db_session: AsyncSession) -> None:
    payload = FieldCreate(name="Lote", latitude=-34.6, longitude=-58.4)

    # FK inexistente (sqlstate 23503): no es un duplicado, así que no debe ser un 409.
    with pytest.raises(IntegrityError):
        await create_field(db_session, 999_999_999_999, payload)
