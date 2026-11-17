"""Tests de `/fields`. Ver docs/TESTING.md §test_fields y docs/API.md §fields."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs.evaluator import evaluate_alerts
from app.models import EventType, Field, Notification
from app.schemas.common import BIGINT_MAX
from app.schemas.field import FieldCreate
from app.services.fields import InvalidTimezoneError, create_field
from tests.conftest import MakeAlert, MakeField, MakeForecast, MakeUser, auth

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
    assert set(response.json()) == {
        "id",
        "name",
        "latitude",
        "longitude",
        "timezone",
        "created_at",
    }


async def test_field_timezone_defaults_to_buenos_aires(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """Omitir `timezone` guarda el default de D9/E7 y lo devuelve en `FieldRead`."""
    user = await make_user()

    response = await client.post(
        API,
        json={"name": "Lote", "latitude": -34.6, "longitude": -58.4},
        headers=auth(user),
    )

    assert response.status_code == 201
    assert response.json()["timezone"] == "America/Argentina/Buenos_Aires"
    field_id = response.json()["id"]
    read = await client.get(f"{API}/{field_id}", headers=auth(user))
    assert read.status_code == 200
    assert read.json()["timezone"] == "America/Argentina/Buenos_Aires"


@pytest.mark.parametrize("timezone", ["UTC", "Asia/Tokyo", "Etc/GMT+3"])
async def test_create_field_persists_a_valid_iana_timezone(
    client: AsyncClient, make_user: MakeUser, timezone: str
) -> None:
    user = await make_user()

    response = await client.post(
        API,
        json={
            "name": f"Lote {timezone}",
            "latitude": -34.6,
            "longitude": -58.4,
            "timezone": timezone,
        },
        headers=auth(user),
    )

    assert response.status_code == 201
    assert response.json()["timezone"] == timezone
    listing = await client.get(API, headers=auth(user))
    assert [item["timezone"] for item in listing.json()["items"]] == [timezone]


@pytest.mark.parametrize(
    "timezone",
    [
        "",
        "   ",
        "Buenos Aires",
        "America/Argentina/../Buenos_Aires",
        "/etc/passwd",
        "../etc/passwd",
        "Mars/Olympus_Mons",
        "UTC+3",
    ],
)
async def test_create_field_with_an_invalid_timezone_returns_422(
    client: AsyncClient, make_user: MakeUser, timezone: str
) -> None:
    user = await make_user()

    response = await client.post(
        API,
        json={
            "name": "Lote",
            "latitude": -34.6,
            "longitude": -58.4,
            "timezone": timezone,
        },
        headers=auth(user),
    )

    assert response.status_code == 422
    fields = response.json()["detail"]
    assert any("timezone" in error["loc"] for error in fields)
    assert "IANA" in fields[0]["msg"]


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


async def test_listing_with_the_maximum_offset_returns_200(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """S12c: `offset=10000` es el tope documentado y sigue siendo válido."""
    user = await make_user()

    response = await client.get(f"{API}?offset=10000", headers=auth(user))

    assert response.status_code == 200
    assert response.json() == {"items": [], "limit": 50, "offset": 10000}


@pytest.mark.parametrize("offset", ["10001", str(BIGINT_MAX + 1), "99999999999999999999"])
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


async def test_the_service_rejects_a_timezone_postgres_does_not_know(
    db_session: AsyncSession, make_user: MakeUser
) -> None:
    """La seguridad no depende del schema: el service valida contra `pg_timezone_names`.

    `model_construct` saltea los validadores de Pydantic (como haría un llamador interno):
    el service igual debe rechazar la zona y no insertar nada (S12a CR-1/SEC-1).
    """
    user = await make_user()
    payload = FieldCreate.model_construct(
        name="Lote",
        latitude=-34.6,
        longitude=-58.4,
        timezone="america/argentina/buenos_aires",
    )

    with pytest.raises(InvalidTimezoneError) as error:
        await create_field(db_session, user.id, payload)

    assert "pg_timezone_names" in str(error.value)
    assert list((await db_session.execute(select(Field))).scalars().all()) == []


# --- Zona IANA: formatos no válidos y alias de la tz database (S12a) --------


def _zoneinfo_resolves(key: str) -> bool:
    """¿La tz database de este SO conoce la clave? Es la misma fuente que usa el schema."""
    try:
        ZoneInfo(key)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


@pytest.mark.parametrize("timezone", ["../../etc/passwd", "GMT-3", "Local"])
async def test_create_field_rejects_non_iana_timezone_formats(
    client: AsyncClient, make_user: MakeUser, timezone: str
) -> None:
    """Rutas con `..`, offsets con signo y el nombre legado de glibc no son zonas IANA."""
    user = await make_user()

    response = await client.post(
        API,
        json={
            "name": "Lote",
            "latitude": -34.6,
            "longitude": -58.4,
            "timezone": timezone,
        },
        headers=auth(user),
    )

    assert response.status_code == 422
    errors = response.json()["detail"]
    assert any("timezone" in error["loc"] for error in errors)


async def test_a_timezone_longer_than_the_column_limit_returns_422(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """El schema limita a 64 caracteres, como `fields.timezone VARCHAR(64)`."""
    user = await make_user()

    response = await client.post(
        API,
        json={
            "name": "Lote",
            "latitude": -34.6,
            "longitude": -58.4,
            "timezone": "A" * 65,
        },
        headers=auth(user),
    )

    assert response.status_code == 422
    assert any("timezone" in error["loc"] for error in response.json()["detail"])


@pytest.mark.parametrize(
    "timezone",
    [
        # Archivo del sistema, no un nombre de la tz database.
        "localtime",
        # Placeholder de tzdata / archivo suelto: Postgres los resuelve pero no son regiones.
        "posixrules",
        "Factory",
        # Unicode: el long s (U+017F) casefoldea a `s` en un filesystem case-insensitive.
        "america/argentina/buenos_aire\u017f",
        # Minúsculas: `pg_timezone_names` compara exacto y case-sensitive.
        "america/argentina/buenos_aires",
        "AMERICA/ARGENTINA/BUENOS_AIRES",
    ],
)
async def test_create_field_rejects_zones_that_postgres_does_not_know_exactly(
    client: AsyncClient, make_user: MakeUser, timezone: str
) -> None:
    """La fuente de verdad es `pg_timezone_names` (S12a CR-1/SEC-1), no `zoneinfo`.

    `zoneinfo` puede resolver estas variantes según el sistema de archivos (macOS las acepta
    por case-folding), pero Postgres no las reconoce con coincidencia exacta: aceptarlas
    rompería `AT TIME ZONE` y con él la evaluación de **todos** los campos.
    """
    user = await make_user()

    response = await client.post(
        API,
        json={
            "name": "Lote",
            "latitude": -34.6,
            "longitude": -58.4,
            "timezone": timezone,
        },
        headers=auth(user),
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    if isinstance(detail, str):
        assert "timezone" in detail.lower()
    else:
        assert any("timezone" in error["loc"] for error in detail)
    assert (await client.get(API, headers=auth(user))).json()["items"] == []


@pytest.mark.parametrize(
    ("timezone", "expected"),
    [
        # Nombre canónico y alias histórico de la tz database: Postgres los conoce.
        ("America/Argentina/Buenos_Aires", 201),
        pytest.param(
            "America/Buenos_Aires",
            201,
            marks=pytest.mark.skipif(
                not _zoneinfo_resolves("America/Buenos_Aires"),
                reason="la tz database de este SO no trae el alias legacy",
            ),
        ),
        ("UTC", 201),
    ],
)
async def test_timezone_acceptance_is_an_exact_match_against_pg_timezone_names(
    client: AsyncClient, make_user: MakeUser, timezone: str, expected: int
) -> None:
    """Cross-check con la fuente real: lo que la API acepta, el evaluador lo puede usar."""
    user = await make_user()

    response = await client.post(
        API,
        json={
            "name": f"Lote {timezone}",
            "latitude": -34.6,
            "longitude": -58.4,
            "timezone": timezone,
        },
        headers=auth(user),
    )

    assert response.status_code == expected
    if expected == 201:
        assert response.json()["timezone"] == timezone


@pytest.mark.skipif(
    not _zoneinfo_resolves("America/Buenos_Aires"),
    reason="la tz database de este SO no trae el alias legacy",
)
async def test_a_legacy_alias_accepted_by_the_api_is_usable_by_the_evaluator(
    client: AsyncClient,
    db_session: AsyncSession,
    make_user: MakeUser,
    make_alert: MakeAlert,
    make_forecast: MakeForecast,
) -> None:
    """Cross-check: si `zoneinfo` acepta el alias, Postgres también sabe interpretarlo.

    El evaluador resuelve el "hoy" con `AT TIME ZONE fields.timezone`: una zona aceptada por
    la API pero desconocida para la tz database de Postgres rompería todos los ciclos.
    """
    user = await make_user()
    response = await client.post(
        API,
        json={
            "name": "Lote alias",
            "latitude": -34.6,
            "longitude": -58.4,
            "timezone": "America/Buenos_Aires",
        },
        headers=auth(user),
    )
    assert response.status_code == 201

    field = await db_session.get(Field, response.json()["id"])
    assert field is not None
    now = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    await make_alert(field, event_type=EventType.FROST, threshold=50)
    await make_forecast(
        field,
        forecast_date=now.date(),
        event_type=EventType.FROST,
        probability=80,
        issued_at=now,
    )

    created = await evaluate_alerts(
        db_session,
        now=now,
        horizon_days=7,
        max_forecast_age_hours=24,
        future_tolerance_minutes=60,
    )

    assert created == 1
    notification = await db_session.scalar(select(Notification))
    assert notification is not None
    assert notification.forecast_date == now.date()
