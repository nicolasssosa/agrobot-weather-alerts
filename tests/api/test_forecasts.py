"""Tests de `/fields/{id}/forecasts`.

Ver docs/TESTING.md §test_forecasts y docs/API.md §forecasts.
"""

from datetime import date, datetime, timedelta

import pytest
from httpx import AsyncClient

from app.models import EventType
from app.schemas.common import BIGINT_MAX
from tests.conftest import MakeField, MakeForecast, MakeUser, auth

DAY = date(2026, 11, 16)


def _url(field_id: int | str) -> str:
    return f"/api/v1/fields/{field_id}/forecasts"


async def test_listing_returns_forecasts_ordered_by_date_and_event_type(
    client: AsyncClient,
    make_user: MakeUser,
    make_field: MakeField,
    make_forecast: MakeForecast,
) -> None:
    user = await make_user()
    field = await make_field(user=user)
    await make_forecast(
        field,
        forecast_date=DAY + timedelta(days=1),
        event_type=EventType.RAIN,
        probability=10,
    )
    await make_forecast(field, forecast_date=DAY, event_type=EventType.STRONG_WIND, probability=20)
    await make_forecast(field, forecast_date=DAY, event_type=EventType.FROST, probability=30)
    await make_forecast(
        field,
        forecast_date=DAY + timedelta(days=2),
        event_type=EventType.HAIL,
        probability=40,
    )

    response = await client.get(_url(field.id), headers=auth(user))

    assert response.status_code == 200
    assert [(item["forecast_date"], item["event_type"]) for item in response.json()] == [
        (DAY.isoformat(), EventType.FROST.value),
        (DAY.isoformat(), EventType.STRONG_WIND.value),
        ((DAY + timedelta(days=1)).isoformat(), EventType.RAIN.value),
        ((DAY + timedelta(days=2)).isoformat(), EventType.HAIL.value),
    ]


async def test_listing_filters_by_event_type(
    client: AsyncClient,
    make_user: MakeUser,
    make_field: MakeField,
    make_forecast: MakeForecast,
) -> None:
    user = await make_user()
    field = await make_field(user=user)
    await make_forecast(field, forecast_date=DAY, event_type=EventType.FROST, probability=10)
    await make_forecast(field, forecast_date=DAY, event_type=EventType.RAIN, probability=20)
    await make_forecast(
        field,
        forecast_date=DAY + timedelta(days=1),
        event_type=EventType.FROST,
        probability=30,
    )

    response = await client.get(f"{_url(field.id)}?event_type=frost", headers=auth(user))

    assert response.status_code == 200
    body = response.json()
    assert [item["event_type"] for item in body] == ["frost", "frost"]
    assert [item["probability"] for item in body] == [10, 30]


@pytest.mark.parametrize("event_type", [event_type.value for event_type in EventType])
async def test_every_event_type_can_be_used_as_a_filter(
    client: AsyncClient,
    make_user: MakeUser,
    make_field: MakeField,
    make_forecast: MakeForecast,
    event_type: str,
) -> None:
    user = await make_user()
    field = await make_field(user=user)
    await make_forecast(field, forecast_date=DAY, event_type=EventType(event_type), probability=50)

    response = await client.get(f"{_url(field.id)}?event_type={event_type}", headers=auth(user))

    assert response.status_code == 200
    assert [item["event_type"] for item in response.json()] == [event_type]


async def test_listing_filters_by_an_inclusive_date_range(
    client: AsyncClient,
    make_user: MakeUser,
    make_field: MakeField,
    make_forecast: MakeForecast,
) -> None:
    user = await make_user()
    field = await make_field(user=user)
    for offset in range(5):
        await make_forecast(
            field,
            forecast_date=DAY + timedelta(days=offset),
            event_type=EventType.FROST,
            probability=offset,
        )

    response = await client.get(
        f"{_url(field.id)}?date_from={(DAY + timedelta(days=1)).isoformat()}"
        f"&date_to={(DAY + timedelta(days=3)).isoformat()}",
        headers=auth(user),
    )

    assert response.status_code == 200
    assert [item["forecast_date"] for item in response.json()] == [
        (DAY + timedelta(days=offset)).isoformat() for offset in (1, 2, 3)
    ]


async def test_listing_filters_by_date_from_only(
    client: AsyncClient,
    make_user: MakeUser,
    make_field: MakeField,
    make_forecast: MakeForecast,
) -> None:
    user = await make_user()
    field = await make_field(user=user)
    for offset in range(3):
        await make_forecast(
            field,
            forecast_date=DAY + timedelta(days=offset),
            event_type=EventType.FROST,
            probability=offset,
        )

    response = await client.get(
        f"{_url(field.id)}?date_from={(DAY + timedelta(days=2)).isoformat()}",
        headers=auth(user),
    )

    assert response.status_code == 200
    assert [item["forecast_date"] for item in response.json()] == [
        (DAY + timedelta(days=2)).isoformat()
    ]


async def test_listing_filters_by_date_to_only(
    client: AsyncClient,
    make_user: MakeUser,
    make_field: MakeField,
    make_forecast: MakeForecast,
) -> None:
    user = await make_user()
    field = await make_field(user=user)
    for offset in range(3):
        await make_forecast(
            field,
            forecast_date=DAY + timedelta(days=offset),
            event_type=EventType.FROST,
            probability=offset,
        )

    response = await client.get(f"{_url(field.id)}?date_to={DAY.isoformat()}", headers=auth(user))

    assert response.status_code == 200
    assert [item["forecast_date"] for item in response.json()] == [DAY.isoformat()]


async def test_equal_date_from_and_date_to_is_accepted(
    client: AsyncClient,
    make_user: MakeUser,
    make_field: MakeField,
    make_forecast: MakeForecast,
) -> None:
    user = await make_user()
    field = await make_field(user=user)
    await make_forecast(field, forecast_date=DAY, event_type=EventType.FROST, probability=10)
    await make_forecast(
        field,
        forecast_date=DAY + timedelta(days=1),
        event_type=EventType.FROST,
        probability=20,
    )

    response = await client.get(
        f"{_url(field.id)}?date_from={DAY.isoformat()}&date_to={DAY.isoformat()}",
        headers=auth(user),
    )

    assert response.status_code == 200
    assert [item["probability"] for item in response.json()] == [10]


async def test_date_from_after_date_to_returns_422(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField
) -> None:
    user = await make_user()
    field = await make_field(user=user)

    response = await client.get(
        f"{_url(field.id)}?date_from=2026-11-20&date_to=2026-11-19", headers=auth(user)
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "date_from must be <= date_to"}


async def test_listing_combines_event_type_with_an_inclusive_date_range(
    client: AsyncClient,
    make_user: MakeUser,
    make_field: MakeField,
    make_forecast: MakeForecast,
) -> None:
    user = await make_user()
    field = await make_field(user=user)
    # `frost` en los 5 días y `rain` solo en el rango pedido: el filtro combinado no debe
    # devolver los frost del rango ni los rain de los bordes.
    for offset in range(5):
        await make_forecast(
            field,
            forecast_date=DAY + timedelta(days=offset),
            event_type=EventType.FROST,
            probability=offset,
        )
    for offset in (1, 2, 3):
        await make_forecast(
            field,
            forecast_date=DAY + timedelta(days=offset),
            event_type=EventType.RAIN,
            probability=50 + offset,
        )

    response = await client.get(
        f"{_url(field.id)}?event_type=rain"
        f"&date_from={(DAY + timedelta(days=1)).isoformat()}"
        f"&date_to={(DAY + timedelta(days=3)).isoformat()}",
        headers=auth(user),
    )

    assert response.status_code == 200
    body = response.json()
    assert [item["event_type"] for item in body] == ["rain", "rain", "rain"]
    assert [item["forecast_date"] for item in body] == [
        (DAY + timedelta(days=offset)).isoformat() for offset in (1, 2, 3)
    ]


async def test_combined_filters_without_matches_return_an_empty_list(
    client: AsyncClient,
    make_user: MakeUser,
    make_field: MakeField,
    make_forecast: MakeForecast,
) -> None:
    user = await make_user()
    field = await make_field(user=user)
    await make_forecast(field, forecast_date=DAY, event_type=EventType.FROST, probability=10)

    response = await client.get(
        f"{_url(field.id)}?event_type=hail&date_from={DAY.isoformat()}&date_to={DAY.isoformat()}",
        headers=auth(user),
    )

    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.parametrize(
    "query",
    [
        "date_from=2026-13-01",
        "date_from=abc",
        "date_to=2026-02-30",
        "date_to=abc",
        "event_type=tornado",
        "event_type=FROST",
        # Años fuera del rango de `date` de Python: 422, nunca 500.
        "date_from=0000-01-01",
        "date_from=10000-01-01",
        "date_to=0000-01-01",
        "date_to=10000-01-01",
        "date_from=0000-01-01&date_to=2026-11-16",
        "date_from=2026-11-16&date_to=10000-01-01",
    ],
)
async def test_invalid_query_parameters_return_422(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField, query: str
) -> None:
    user = await make_user()
    field = await make_field(user=user)

    response = await client.get(f"{_url(field.id)}?{query}", headers=auth(user))

    assert response.status_code == 422


async def test_forecasts_of_a_field_of_another_user_return_404(
    client: AsyncClient,
    make_user: MakeUser,
    make_field: MakeField,
    make_forecast: MakeForecast,
) -> None:
    owner = await make_user()
    intruder = await make_user()
    field = await make_field(user=owner)
    await make_forecast(field, forecast_date=DAY, event_type=EventType.FROST, probability=10)

    own = await client.get(_url(field.id), headers=auth(owner))
    other = await client.get(_url(field.id), headers=auth(intruder))

    assert own.status_code == 200
    assert other.status_code == 404
    assert other.json() == {"detail": "Field not found"}


@pytest.mark.parametrize(
    "field_id",
    ["999999999", str(BIGINT_MAX + 1), "99999999999999999999", "-1"],
)
async def test_unknown_or_out_of_range_field_returns_404(
    client: AsyncClient, make_user: MakeUser, field_id: str
) -> None:
    user = await make_user()

    response = await client.get(_url(field_id), headers=auth(user))

    assert response.status_code == 404
    assert response.json() == {"detail": "Field not found"}


async def test_non_numeric_field_id_returns_422(client: AsyncClient, make_user: MakeUser) -> None:
    user = await make_user()

    response = await client.get(_url("abc"), headers=auth(user))

    assert response.status_code == 422


async def test_listing_without_header_returns_401(client: AsyncClient) -> None:
    response = await client.get(_url(1))

    assert response.status_code == 401
    assert response.json() == {"detail": "Missing or invalid X-User-Id header"}


async def test_field_without_forecasts_returns_an_empty_list(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField
) -> None:
    user = await make_user()
    field = await make_field(user=user)

    response = await client.get(_url(field.id), headers=auth(user))

    assert response.status_code == 200
    assert response.json() == []


async def test_listing_only_returns_forecasts_of_the_requested_field(
    client: AsyncClient,
    make_user: MakeUser,
    make_field: MakeField,
    make_forecast: MakeForecast,
) -> None:
    user = await make_user()
    field = await make_field(user=user)
    other_field = await make_field(user=user)
    await make_forecast(field, forecast_date=DAY, event_type=EventType.FROST, probability=10)
    await make_forecast(other_field, forecast_date=DAY, event_type=EventType.FROST, probability=99)

    response = await client.get(_url(field.id), headers=auth(user))

    assert response.status_code == 200
    assert [item["probability"] for item in response.json()] == [10]


async def test_response_exposes_only_the_documented_fields(
    client: AsyncClient,
    make_user: MakeUser,
    make_field: MakeField,
    make_forecast: MakeForecast,
) -> None:
    user = await make_user()
    field = await make_field(user=user)
    forecast = await make_forecast(
        field, forecast_date=DAY, event_type=EventType.HEAT_WAVE, probability=72, source="mock"
    )

    response = await client.get(_url(field.id), headers=auth(user))

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    item = body[0]
    assert set(item) == {"forecast_date", "event_type", "probability", "source", "fetched_at"}
    assert item["forecast_date"] == DAY.isoformat()
    assert item["event_type"] == "heat_wave"
    assert item["probability"] == 72
    assert item["source"] == "mock"
    fetched_at = datetime.fromisoformat(item["fetched_at"].replace("Z", "+00:00"))
    assert fetched_at.tzinfo is not None
    assert abs((fetched_at - forecast.fetched_at).total_seconds()) < 1


@pytest.mark.parametrize(
    "query",
    [
        "event_type=frost&event_type=rain",
        f"date_from={DAY.isoformat()}&date_from={(DAY + timedelta(days=2)).isoformat()}",
        f"date_to={(DAY + timedelta(days=2)).isoformat()}&date_to={DAY.isoformat()}",
        f"date_from={DAY.isoformat()}&date_from={(DAY + timedelta(days=1)).isoformat()}"
        f"&date_to={(DAY + timedelta(days=2)).isoformat()}",
    ],
)
async def test_repeated_query_parameters_do_not_error(
    client: AsyncClient,
    make_user: MakeUser,
    make_field: MakeField,
    make_forecast: MakeForecast,
    query: str,
) -> None:
    user = await make_user()
    field = await make_field(user=user)
    for offset in range(3):
        for event_type in (EventType.FROST, EventType.RAIN):
            await make_forecast(
                field,
                forecast_date=DAY + timedelta(days=offset),
                event_type=event_type,
                probability=offset,
            )

    response = await client.get(f"{_url(field.id)}?{query}", headers=auth(user))

    # FastAPI aplica el último valor; lo que importa acá es que un query repetido no sea 500.
    assert response.status_code == 200
    assert isinstance(response.json(), list)


async def test_date_range_is_validated_before_ownership(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField
) -> None:
    owner = await make_user()
    intruder = await make_user()
    field = await make_field(user=owner)

    response = await client.get(
        f"{_url(field.id)}?date_from=2026-11-20&date_to=2026-11-19", headers=auth(intruder)
    )

    # El 422 del query param gana sobre el 404 del ownership (docs/API.md §forecasts).
    assert response.status_code == 422
    assert response.json() == {"detail": "date_from must be <= date_to"}
