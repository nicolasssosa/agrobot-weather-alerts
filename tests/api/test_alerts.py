"""Tests de `/alerts`. Ver docs/TESTING.md §test_alerts y docs/API.md §alerts."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.db import get_session
from app.core.errors import ConflictError
from app.main import create_app
from app.models import Alert, EventType, Field, Notification, User
from app.schemas.alert import AlertCreate
from app.schemas.common import BIGINT_MAX
from app.services import alerts as alerts_service
from app.services.alerts import create_alert
from tests.conftest import TEST_DATABASE_URL, MakeAlert, MakeField, MakeUser, auth

API = "/api/v1/alerts"
OLD_UPDATED_AT = datetime(2020, 1, 1, tzinfo=UTC)


async def test_create_alert_returns_201_with_is_active_true(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField
) -> None:
    user = await make_user()
    field = await make_field(user=user)

    response = await client.post(
        API,
        json={"field_id": field.id, "event_type": "frost", "threshold": 70},
        headers=auth(user),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["id"] > 0
    assert body["field_id"] == field.id
    assert body["event_type"] == "frost"
    assert body["threshold"] == 70
    assert body["is_active"] is True
    assert body["created_at"]
    assert body["updated_at"]


async def test_created_alert_appears_in_the_listing(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField
) -> None:
    user = await make_user()
    field = await make_field(user=user)

    created = await client.post(
        API,
        json={"field_id": field.id, "event_type": "rain", "threshold": 40},
        headers=auth(user),
    )
    listing = await client.get(API, headers=auth(user))

    assert listing.status_code == 200
    assert listing.json()["items"] == [created.json()]


async def test_duplicate_field_and_event_type_returns_409(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField
) -> None:
    user = await make_user()
    field = await make_field(user=user)
    payload = {"field_id": field.id, "event_type": "frost", "threshold": 70}
    assert (await client.post(API, json=payload, headers=auth(user))).status_code == 201

    response = await client.post(API, json=payload, headers=auth(user))

    assert response.status_code == 409
    assert response.json() == {"detail": "An alert for this field and event type already exists"}


async def test_same_event_type_on_another_field_is_accepted(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField
) -> None:
    user = await make_user()
    first = await make_field(user=user)
    second = await make_field(user=user)
    payload = {"event_type": "frost", "threshold": 70}
    assert (
        await client.post(API, json={**payload, "field_id": first.id}, headers=auth(user))
    ).status_code == 201

    response = await client.post(API, json={**payload, "field_id": second.id}, headers=auth(user))

    assert response.status_code == 201


async def test_another_event_type_on_the_same_field_is_accepted(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField
) -> None:
    user = await make_user()
    field = await make_field(user=user)
    assert (
        await client.post(
            API,
            json={"field_id": field.id, "event_type": "frost", "threshold": 70},
            headers=auth(user),
        )
    ).status_code == 201

    response = await client.post(
        API,
        json={"field_id": field.id, "event_type": "hail", "threshold": 70},
        headers=auth(user),
    )

    assert response.status_code == 201


async def test_creating_an_alert_on_another_users_field_returns_404(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField
) -> None:
    owner = await make_user()
    intruder = await make_user()
    field = await make_field(user=owner)

    response = await client.post(
        API,
        json={"field_id": field.id, "event_type": "frost", "threshold": 70},
        headers=auth(intruder),
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Field not found"}


async def test_creating_an_alert_on_a_missing_field_returns_404(
    client: AsyncClient, make_user: MakeUser
) -> None:
    user = await make_user()

    response = await client.post(
        API,
        json={"field_id": 999999999, "event_type": "frost", "threshold": 70},
        headers=auth(user),
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Field not found"}


@pytest.mark.parametrize(
    "field_id",
    [str(BIGINT_MAX + 1), "99999999999999999999", "-1", "0"],
)
async def test_creating_an_alert_with_an_out_of_range_field_id_returns_404(
    client: AsyncClient, make_user: MakeUser, field_id: str
) -> None:
    user = await make_user()

    response = await client.post(
        API,
        content=f'{{"field_id": {field_id}, "event_type": "frost", "threshold": 70}}',
        headers={**auth(user), "content-type": "application/json"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Field not found"}


@pytest.mark.parametrize("threshold", [0, 101, -1, True, False, 70.0, "70", None])
async def test_invalid_threshold_returns_422(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField, threshold: object
) -> None:
    user = await make_user()
    field = await make_field(user=user)

    response = await client.post(
        API,
        json={"field_id": field.id, "event_type": "frost", "threshold": threshold},
        headers=auth(user),
    )

    assert response.status_code == 422


@pytest.mark.parametrize("threshold", [1, 100])
async def test_threshold_boundaries_are_accepted(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField, threshold: int
) -> None:
    user = await make_user()
    field = await make_field(user=user)

    response = await client.post(
        API,
        json={"field_id": field.id, "event_type": "frost", "threshold": threshold},
        headers=auth(user),
    )

    assert response.status_code == 201
    assert response.json()["threshold"] == threshold


@pytest.mark.parametrize("event_type", ["FROST", "tornado", "", None])
async def test_invalid_event_type_returns_422(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField, event_type: object
) -> None:
    user = await make_user()
    field = await make_field(user=user)

    response = await client.post(
        API,
        json={"field_id": field.id, "event_type": event_type, "threshold": 70},
        headers=auth(user),
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "extra",
    [{"is_active": False}, {"unknown": 1}],
)
async def test_unknown_or_read_only_fields_in_the_create_payload_return_422(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField, extra: dict[str, object]
) -> None:
    user = await make_user()
    field = await make_field(user=user)

    response = await client.post(
        API,
        json={"field_id": field.id, "event_type": "frost", "threshold": 70, **extra},
        headers=auth(user),
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"event_type": "frost", "threshold": 70},
        {"field_id": 1, "threshold": 70},
        {"field_id": 1, "event_type": "frost"},
    ],
)
async def test_missing_required_fields_return_422(
    client: AsyncClient, make_user: MakeUser, payload: dict[str, object]
) -> None:
    user = await make_user()

    response = await client.post(API, json=payload, headers=auth(user))

    assert response.status_code == 422


async def test_listing_filters_by_field_id(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField, make_alert: MakeAlert
) -> None:
    user = await make_user()
    field = await make_field(user=user)
    other_field = await make_field(user=user)
    alert = await make_alert(field, event_type=EventType.FROST)
    await make_alert(other_field, event_type=EventType.FROST)

    response = await client.get(f"{API}?field_id={field.id}", headers=auth(user))

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [alert.id]


async def test_listing_filters_by_event_type(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField, make_alert: MakeAlert
) -> None:
    user = await make_user()
    field = await make_field(user=user)
    await make_alert(field, event_type=EventType.FROST)
    rain = await make_alert(field, event_type=EventType.RAIN)

    response = await client.get(f"{API}?event_type=rain", headers=auth(user))

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [rain.id]


async def test_listing_filters_by_is_active(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField, make_alert: MakeAlert
) -> None:
    user = await make_user()
    field = await make_field(user=user)
    await make_alert(field, event_type=EventType.FROST, is_active=True)
    inactive = await make_alert(field, event_type=EventType.RAIN, is_active=False)

    response = await client.get(f"{API}?is_active=false", headers=auth(user))

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [inactive.id]


async def test_listing_combines_all_filters(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField, make_alert: MakeAlert
) -> None:
    user = await make_user()
    field = await make_field(user=user)
    other_field = await make_field(user=user)
    await make_alert(field, event_type=EventType.RAIN, is_active=True)
    expected = await make_alert(field, event_type=EventType.FROST, is_active=False)
    await make_alert(other_field, event_type=EventType.FROST, is_active=False)

    response = await client.get(
        f"{API}?field_id={field.id}&event_type=frost&is_active=false", headers=auth(user)
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [expected.id]


async def test_listing_is_paginated_and_ordered_by_id(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField, make_alert: MakeAlert
) -> None:
    user = await make_user()
    field = await make_field(user=user)
    alerts = [
        await make_alert(field, event_type=event_type)
        for event_type in (EventType.FROST, EventType.RAIN, EventType.HAIL)
    ]

    response = await client.get(f"{API}?limit=2&offset=1", headers=auth(user))

    assert response.status_code == 200
    body = response.json()
    assert body["limit"] == 2
    assert body["offset"] == 1
    assert [item["id"] for item in body["items"]] == [alerts[1].id, alerts[2].id]


async def test_listing_defaults_to_limit_50_offset_0(
    client: AsyncClient, make_user: MakeUser
) -> None:
    user = await make_user()

    response = await client.get(API, headers=auth(user))

    assert response.status_code == 200
    assert response.json() == {"items": [], "limit": 50, "offset": 0}


async def test_listing_with_an_offset_past_the_end_returns_an_empty_page(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField, make_alert: MakeAlert
) -> None:
    user = await make_user()
    field = await make_field(user=user)
    await make_alert(field)

    response = await client.get(f"{API}?limit=10&offset=5", headers=auth(user))

    assert response.status_code == 200
    assert response.json() == {"items": [], "limit": 10, "offset": 5}


async def test_listing_returns_only_the_authenticated_users_alerts(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField, make_alert: MakeAlert
) -> None:
    owner = await make_user()
    other = await make_user()
    own_field = await make_field(user=owner)
    other_field = await make_field(user=other)
    own = await make_alert(own_field, event_type=EventType.FROST)
    await make_alert(other_field, event_type=EventType.FROST)

    response = await client.get(API, headers=auth(owner))

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [own.id]


async def test_listing_filtering_by_another_users_field_returns_an_empty_page(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField, make_alert: MakeAlert
) -> None:
    owner = await make_user()
    intruder = await make_user()
    field = await make_field(user=owner)
    await make_alert(field, event_type=EventType.FROST)

    response = await client.get(f"{API}?field_id={field.id}", headers=auth(intruder))

    assert response.status_code == 200
    assert response.json()["items"] == []


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=101",
        "offset=-1",
        "limit=abc",
        "field_id=0",
        "field_id=-1",
        f"field_id={BIGINT_MAX + 1}",
        "field_id=99999999999999999999",
        "field_id=abc",
        "event_type=tornado",
        "event_type=FROST",
        "is_active=abc",
    ],
)
async def test_invalid_listing_query_parameters_return_422(
    client: AsyncClient, make_user: MakeUser, query: str
) -> None:
    user = await make_user()

    response = await client.get(f"{API}?{query}", headers=auth(user))

    assert response.status_code == 422


async def test_reading_an_own_alert_returns_200(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField, make_alert: MakeAlert
) -> None:
    user = await make_user()
    field = await make_field(user=user)
    alert = await make_alert(field, event_type=EventType.HEAT_WAVE, threshold=85)

    response = await client.get(f"{API}/{alert.id}", headers=auth(user))

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == alert.id
    assert body["event_type"] == "heat_wave"
    assert body["threshold"] == 85


async def test_reading_another_users_alert_returns_404(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField, make_alert: MakeAlert
) -> None:
    owner = await make_user()
    intruder = await make_user()
    field = await make_field(user=owner)
    alert = await make_alert(field)

    response = await client.get(f"{API}/{alert.id}", headers=auth(intruder))

    assert response.status_code == 404
    assert response.json() == {"detail": "Alert not found"}


@pytest.mark.parametrize(
    "alert_id",
    ["999999999", str(BIGINT_MAX + 1), "99999999999999999999", "-1", "0"],
)
async def test_reading_an_unknown_or_out_of_range_alert_returns_404(
    client: AsyncClient, make_user: MakeUser, alert_id: str
) -> None:
    user = await make_user()

    response = await client.get(f"{API}/{alert_id}", headers=auth(user))

    assert response.status_code == 404
    assert response.json() == {"detail": "Alert not found"}


async def test_non_numeric_alert_id_returns_422(client: AsyncClient, make_user: MakeUser) -> None:
    user = await make_user()

    response = await client.get(f"{API}/abc", headers=auth(user))

    assert response.status_code == 422


async def test_patch_threshold_returns_200_and_updates_the_row(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField, make_alert: MakeAlert
) -> None:
    user = await make_user()
    field = await make_field(user=user)
    alert = await make_alert(field, threshold=70)

    response = await client.patch(f"{API}/{alert.id}", json={"threshold": 90}, headers=auth(user))

    assert response.status_code == 200
    body = response.json()
    assert body["threshold"] == 90
    assert body["is_active"] is True
    assert body["field_id"] == alert.field_id
    assert body["event_type"] == alert.event_type


async def test_patch_is_active_returns_200(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField, make_alert: MakeAlert
) -> None:
    user = await make_user()
    field = await make_field(user=user)
    alert = await make_alert(field)

    response = await client.patch(
        f"{API}/{alert.id}", json={"is_active": False}, headers=auth(user)
    )

    assert response.status_code == 200
    assert response.json()["is_active"] is False


async def test_patch_both_fields_returns_200(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField, make_alert: MakeAlert
) -> None:
    user = await make_user()
    field = await make_field(user=user)
    alert = await make_alert(field, threshold=70, is_active=True)

    response = await client.patch(
        f"{API}/{alert.id}", json={"threshold": 50, "is_active": False}, headers=auth(user)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["threshold"] == 50
    assert body["is_active"] is False


async def test_patch_updates_updated_at(
    client: AsyncClient,
    db_session: AsyncSession,
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    user = await make_user()
    field = await make_field(user=user)
    alert = await make_alert(field, threshold=70)
    # Postgres `now()` es el inicio de la transacción: dentro del test created_at y
    # updated_at coinciden. Envejecemos updated_at para probar el `onupdate=func.now()`.
    await db_session.execute(
        update(Alert).where(Alert.id == alert.id).values(updated_at=OLD_UPDATED_AT)
    )
    await db_session.refresh(alert)
    assert alert.updated_at == OLD_UPDATED_AT

    response = await client.patch(f"{API}/{alert.id}", json={"threshold": 90}, headers=auth(user))

    assert response.status_code == 200
    await db_session.refresh(alert)
    # `now()` es transaction_timestamp: en la misma transacción el valor nuevo coincide con
    # created_at. Lo que prueba el test es que el `onupdate` disparó y pisó el valor viejo.
    assert alert.updated_at > OLD_UPDATED_AT


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"threshold": None},
        {"is_active": None},
        {"threshold": 0},
        {"threshold": 101},
        {"threshold": True},
        {"threshold": 70.0},
        {"threshold": "70"},
        {"is_active": "abc"},
        {"field_id": 1},
        {"event_type": "rain"},
        {"unknown": 1},
    ],
)
async def test_invalid_patch_payloads_return_422(
    client: AsyncClient,
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
    payload: dict[str, object],
) -> None:
    user = await make_user()
    field = await make_field(user=user)
    alert = await make_alert(field)

    response = await client.patch(f"{API}/{alert.id}", json=payload, headers=auth(user))

    assert response.status_code == 422


async def test_patch_of_another_users_alert_returns_404(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField, make_alert: MakeAlert
) -> None:
    owner = await make_user()
    intruder = await make_user()
    field = await make_field(user=owner)
    alert = await make_alert(field, threshold=70)

    response = await client.patch(
        f"{API}/{alert.id}", json={"threshold": 90}, headers=auth(intruder)
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Alert not found"}


@pytest.mark.parametrize(
    "alert_id",
    ["999999999", str(BIGINT_MAX + 1), "99999999999999999999", "-1"],
)
async def test_patch_of_an_unknown_or_out_of_range_alert_returns_404(
    client: AsyncClient, make_user: MakeUser, alert_id: str
) -> None:
    user = await make_user()

    response = await client.patch(f"{API}/{alert_id}", json={"threshold": 90}, headers=auth(user))

    assert response.status_code == 404
    assert response.json() == {"detail": "Alert not found"}


async def test_delete_returns_204_without_body_and_the_alert_disappears(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField, make_alert: MakeAlert
) -> None:
    user = await make_user()
    field = await make_field(user=user)
    alert = await make_alert(field)

    response = await client.delete(f"{API}/{alert.id}", headers=auth(user))

    assert response.status_code == 204
    assert response.content == b""
    assert (await client.get(f"{API}/{alert.id}", headers=auth(user))).status_code == 404


async def test_delete_cascades_to_the_alert_notifications(
    client: AsyncClient,
    db_session: AsyncSession,
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    user = await make_user()
    field = await make_field(user=user)
    alert = await make_alert(field, event_type=EventType.FROST, threshold=70)
    notification = Notification(
        alert_id=alert.id,
        forecast_date=date(2026, 11, 20),
        event_type=EventType.FROST,
        probability=90,
        threshold=70,
    )
    db_session.add(notification)
    await db_session.flush()

    response = await client.delete(f"{API}/{alert.id}", headers=auth(user))

    assert response.status_code == 204
    count = await db_session.execute(
        select(func.count()).select_from(Notification).where(Notification.alert_id == alert.id)
    )
    assert count.scalar_one() == 0


async def test_delete_of_another_users_alert_returns_404_and_keeps_it(
    client: AsyncClient,
    db_session: AsyncSession,
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    owner = await make_user()
    intruder = await make_user()
    field = await make_field(user=owner)
    alert = await make_alert(field)

    response = await client.delete(f"{API}/{alert.id}", headers=auth(intruder))

    assert response.status_code == 404
    assert response.json() == {"detail": "Alert not found"}
    remaining = await db_session.execute(select(Alert).where(Alert.id == alert.id))
    assert remaining.scalar_one_or_none() is not None


@pytest.mark.parametrize(
    "alert_id",
    ["999999999", str(BIGINT_MAX + 1), "99999999999999999999", "-1"],
)
async def test_delete_of_an_unknown_or_out_of_range_alert_returns_404(
    client: AsyncClient, make_user: MakeUser, alert_id: str
) -> None:
    user = await make_user()

    response = await client.delete(f"{API}/{alert_id}", headers=auth(user))

    assert response.status_code == 404
    assert response.json() == {"detail": "Alert not found"}


async def test_alert_read_exposes_only_the_documented_fields(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField
) -> None:
    user = await make_user()
    field = await make_field(user=user)

    response = await client.post(
        API,
        json={"field_id": field.id, "event_type": "strong_wind", "threshold": 60},
        headers=auth(user),
    )

    assert response.status_code == 201
    assert set(response.json()) == {
        "id",
        "field_id",
        "event_type",
        "threshold",
        "is_active",
        "created_at",
        "updated_at",
    }


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("post", API, {"field_id": 1, "event_type": "frost", "threshold": 70}),
        ("get", API, None),
        ("get", f"{API}/1", None),
        ("patch", f"{API}/1", {"threshold": 90}),
        ("delete", f"{API}/1", None),
    ],
)
async def test_endpoints_require_the_auth_header(
    client: AsyncClient, method: str, path: str, payload: dict[str, object] | None
) -> None:
    response = await client.request(method, path, json=payload)

    assert response.status_code == 401
    assert response.json() == {"detail": "Missing or invalid X-User-Id header"}


async def test_non_unique_integrity_error_is_not_mapped_to_409(
    db_session: AsyncSession,
    make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await make_user()
    # Campo "fantasma" (no existe en la DB) para forzar una violación de FK (23503).
    ghost = Field(
        id=BIGINT_MAX,
        user_id=user.id,
        name="Ghost",
        latitude=Decimal("-34.6"),
        longitude=Decimal("-58.4"),
    )

    async def _fake_get_field(*_args: object, **_kwargs: object) -> Field:
        return ghost

    monkeypatch.setattr("app.services.alerts.get_field", _fake_get_field)
    payload = AlertCreate(field_id=BIGINT_MAX, event_type=EventType.FROST, threshold=70)

    with pytest.raises(IntegrityError):
        await create_alert(db_session, user.id, payload)


async def test_the_409_comes_from_the_unique_violation_of_the_insert(
    make_user: MakeUser,
    make_field: MakeField,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El duplicado se detecta capturando el IntegrityError del INSERT (no un SELECT previo).

    Con un `commit` que falla con sqlstate 23505 (y `get_field` resuelto), el service solo
    puede responder `ConflictError` si captura la violación de unicidad en el commit.
    """
    user = await make_user()
    field = await make_field(user=user)
    payload = AlertCreate(field_id=field.id, event_type=EventType.FROST, threshold=70)
    unique_violation = IntegrityError("INSERT", {}, SimpleNamespace(sqlstate="23505"))

    class _FailingCommitSession:
        rolled_back = False

        def add(self, instance: object) -> None:
            pass

        async def commit(self) -> None:
            raise unique_violation

        async def rollback(self) -> None:
            self.rolled_back = True

    async def _fake_get_field(*_args: object, **_kwargs: object) -> Field:
        return field

    monkeypatch.setattr("app.services.alerts.get_field", _fake_get_field)
    session = _FailingCommitSession()

    with pytest.raises(ConflictError) as exc_info:
        # Sesión duck-typed: el test solo ejercita add/commit/rollback.
        await create_alert(session, user.id, payload)  # type: ignore[arg-type]

    assert exc_info.value.detail == "An alert for this field and event type already exists"
    assert session.rolled_back is True


async def test_two_users_can_alert_on_the_same_event_type_in_their_own_fields(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField
) -> None:
    # El UNIQUE es (field_id, event_type): el mismo evento en campos de dueños distintos
    # no es un duplicado.
    first_owner = await make_user()
    second_owner = await make_user()
    first_field = await make_field(user=first_owner)
    second_field = await make_field(user=second_owner)
    payload = {"event_type": "frost", "threshold": 70}

    first = await client.post(
        API, json={**payload, "field_id": first_field.id}, headers=auth(first_owner)
    )
    second = await client.post(
        API, json={**payload, "field_id": second_field.id}, headers=auth(second_owner)
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] != second.json()["id"]
    first_listing = await client.get(API, headers=auth(first_owner))
    second_listing = await client.get(API, headers=auth(second_owner))
    assert [item["id"] for item in first_listing.json()["items"]] == [first.json()["id"]]
    assert [item["id"] for item in second_listing.json()["items"]] == [second.json()["id"]]


async def test_an_alert_can_be_recreated_after_it_is_deleted(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField
) -> None:
    user = await make_user()
    field = await make_field(user=user)
    payload = {"field_id": field.id, "event_type": "frost", "threshold": 70}
    first = await client.post(API, json=payload, headers=auth(user))
    assert first.status_code == 201
    deleted = await client.delete(f"{API}/{first.json()['id']}", headers=auth(user))
    assert deleted.status_code == 204

    second = await client.post(API, json=payload, headers=auth(user))

    assert second.status_code == 201
    assert second.json()["id"] != first.json()["id"]
    # La anterior sigue borrada y el listado tiene una sola alerta (la nueva).
    assert (await client.get(f"{API}/{first.json()['id']}", headers=auth(user))).status_code == 404
    listing = await client.get(API, headers=auth(user))
    assert listing.json()["items"] == [second.json()]


async def test_patch_with_the_same_threshold_is_a_200_noop(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField, make_alert: MakeAlert
) -> None:
    user = await make_user()
    field = await make_field(user=user)
    alert = await make_alert(field, threshold=70, is_active=True)

    response = await client.patch(f"{API}/{alert.id}", json={"threshold": 70}, headers=auth(user))

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == alert.id
    assert body["threshold"] == 70
    assert body["is_active"] is True


async def test_patch_is_active_false_is_reflected_in_the_filtered_listing(
    client: AsyncClient, make_user: MakeUser, make_field: MakeField, make_alert: MakeAlert
) -> None:
    user = await make_user()
    field = await make_field(user=user)
    active = await make_alert(field, event_type=EventType.FROST)
    to_disable = await make_alert(field, event_type=EventType.RAIN)

    patched = await client.patch(
        f"{API}/{to_disable.id}", json={"is_active": False}, headers=auth(user)
    )
    assert patched.status_code == 200
    assert patched.json()["is_active"] is False

    still_active = await client.get(f"{API}?is_active=true", headers=auth(user))
    disabled = await client.get(f"{API}?is_active=false", headers=auth(user))
    assert [item["id"] for item in still_active.json()["items"]] == [active.id]
    assert [item["id"] for item in disabled.json()["items"]] == [to_disable.id]


@pytest.mark.parametrize(
    ("method", "path_template", "payload"),
    [
        ("get", "", None),
        ("get", "/{alert_id}", None),
        ("post", "", {"field_id": 1, "event_type": "frost", "threshold": 70}),
        ("patch", "/{alert_id}", {"threshold": 90}),
        ("delete", "/{alert_id}", None),
    ],
)
async def test_a_deleted_users_header_returns_401(
    client: AsyncClient,
    db_session: AsyncSession,
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
    method: str,
    path_template: str,
    payload: dict[str, object] | None,
) -> None:
    user = await make_user()
    field = await make_field(user=user)
    alert = await make_alert(field)
    # Borrar el usuario (cascadea a campos y alertas) deja el header apuntando a la nada.
    await db_session.execute(delete(User).where(User.id == user.id))
    await db_session.flush()

    response = await client.request(
        method, f"{API}{path_template.format(alert_id=alert.id)}", json=payload, headers=auth(user)
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Missing or invalid X-User-Id header"}


async def test_patch_racing_a_concurrent_delete_returns_404(
    client: AsyncClient,
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CR-2: un DELETE concurrente entre el SELECT y el UPDATE no debe ser un 500.

    Se simula de forma determinista: `get_alert` devuelve la alerta y borra su fila con un
    DELETE de Core. El UPDATE posterior afecta 0 filas -> StaleDataError, que el service
    traduce en 404 (mismo contrato que una alerta inexistente).
    """
    user = await make_user()
    field = await make_field(user=user)
    alert = await make_alert(field)
    original_get_alert = alerts_service.get_alert

    async def get_alert_then_delete(session: AsyncSession, user_id: int, alert_id: int) -> Alert:
        found = await original_get_alert(session, user_id, alert_id)
        # `synchronize_session=False`: el objeto sigue "persistent" en la sesión (como en la
        # carrera real con otra transacción), así el UPDATE afecta 0 filas.
        await session.execute(
            delete(Alert).where(Alert.id == found.id).execution_options(synchronize_session=False)
        )
        return found

    monkeypatch.setattr(alerts_service, "get_alert", get_alert_then_delete)

    response = await client.patch(f"{API}/{alert.id}", json={"threshold": 90}, headers=auth(user))

    assert response.status_code == 404
    assert response.json() == {"detail": "Alert not found"}


async def test_concurrent_identical_posts_produce_exactly_one_alert() -> None:
    """Dos POST simultáneos del mismo (campo, evento): exactamente un 201 y un 409.

    No sirve `client`/`db_session`: esa sesión está atada a una única conexión (no admite
    requests concurrentes) y su transacción se revierte al final, así que otra conexión no
    vería el campo sin commitear. Como en docs/TESTING.md §Concurrencia se usa un engine
    propio, datos commiteados y limpieza en el `finally`.
    """
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    # CR-3: teléfono único por corrida y limpieza incondicional por teléfono (no por
    # user_id): una corrida previa interrumpida no deja el test en rojo para siempre.
    phone_number = f"+54{uuid4().int % 10**12:012d}"
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

        def build_client() -> AsyncClient:
            app = create_app()

            async def _override_session() -> AsyncIterator[AsyncSession]:
                async with sessionmaker() as session:
                    yield session

            app.dependency_overrides[get_session] = _override_session
            return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

        payload = {"field_id": field_id, "event_type": "frost", "threshold": 70}
        headers = {"X-User-Id": str(user_id)}
        async with build_client() as first, build_client() as second:
            first_response, second_response = await asyncio.gather(
                first.post(API, json=payload, headers=headers),
                second.post(API, json=payload, headers=headers),
            )

        assert sorted([first_response.status_code, second_response.status_code]) == [201, 409]
        conflict = first_response if first_response.status_code == 409 else second_response
        assert conflict.json() == {
            "detail": "An alert for this field and event type already exists"
        }
        async with sessionmaker() as session:
            count = await session.scalar(
                select(func.count()).select_from(Alert).where(Alert.field_id == field_id)
            )
        assert count == 1
    finally:
        async with sessionmaker() as session:
            await session.execute(delete(User).where(User.phone_number == phone_number))
            await session.commit()
        await engine.dispose()
