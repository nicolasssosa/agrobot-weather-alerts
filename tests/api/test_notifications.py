"""Tests de `/notifications`. Ver docs/TESTING.md §test_notifications (API.md §notifications)."""

import logging
from datetime import UTC, date, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Alert, EventType, Notification, NotificationStatus, User
from app.notifiers.base import NotificationMessage
from app.notifiers.log import LoggingNotifier
from app.schemas.common import BIGINT_MAX
from tests.conftest import MakeAlert, MakeField, MakeUser, auth

API = "/api/v1/notifications"
FORECAST_DATE = date(2026, 11, 18)
NEXT_ATTEMPT_AT = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


async def _add_notification(
    session: AsyncSession,
    alert: Alert,
    *,
    forecast_date: date = FORECAST_DATE,
    status: NotificationStatus = NotificationStatus.PENDING,
    probability: int = 85,
) -> Notification:
    notification = Notification(
        alert_id=alert.id,
        forecast_date=forecast_date,
        event_type=alert.event_type,
        probability=probability,
        threshold=alert.threshold,
        status=status,
        attempts=0,
        next_attempt_at=NEXT_ATTEMPT_AT,
    )
    session.add(notification)
    await session.flush()
    return notification


async def test_lists_only_the_authenticated_users_notifications(
    client: AsyncClient,
    db_session: AsyncSession,
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    owner = await make_user()
    other = await make_user()
    own_alert = await make_alert(await make_field(owner), event_type=EventType.FROST)
    other_alert = await make_alert(await make_field(other), event_type=EventType.RAIN)
    own = await _add_notification(db_session, own_alert)
    await _add_notification(db_session, other_alert)

    response = await client.get(API, headers=auth(owner))

    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body["items"]] == [own.id]
    assert body["limit"] == 50
    assert body["offset"] == 0


async def test_filters_by_status(
    client: AsyncClient,
    db_session: AsyncSession,
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    user = await make_user()
    alert = await make_alert(await make_field(user), event_type=EventType.FROST)
    pending = await _add_notification(db_session, alert, status=NotificationStatus.PENDING)
    await _add_notification(
        db_session, alert, forecast_date=date(2026, 11, 19), status=NotificationStatus.SENT
    )
    await _add_notification(
        db_session, alert, forecast_date=date(2026, 11, 20), status=NotificationStatus.FAILED
    )

    response = await client.get(f"{API}?status=pending", headers=auth(user))

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [pending.id]


async def test_filters_by_cancelled_status(
    client: AsyncClient,
    db_session: AsyncSession,
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    """S9: `cancelled` es un estado filtrable del historial."""
    user = await make_user()
    alert = await make_alert(await make_field(user), event_type=EventType.FROST)
    cancelled = await _add_notification(db_session, alert, status=NotificationStatus.CANCELLED)
    await _add_notification(db_session, alert, forecast_date=date(2026, 11, 19))

    response = await client.get(f"{API}?status=cancelled", headers=auth(user))

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [cancelled.id]
    assert response.json()["items"][0]["status"] == "cancelled"


async def test_the_history_of_a_soft_deleted_alert_stays_visible(
    client: AsyncClient,
    db_session: AsyncSession,
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    user = await make_user()
    alert = await make_alert(await make_field(user), event_type=EventType.FROST)
    sent = await _add_notification(db_session, alert, status=NotificationStatus.SENT)
    pending = await _add_notification(
        db_session, alert, forecast_date=date(2026, 11, 19), status=NotificationStatus.PENDING
    )

    deleted = await client.delete(f"/api/v1/alerts/{alert.id}", headers=auth(user))

    assert deleted.status_code == 204
    history = await client.get(f"{API}?alert_id={alert.id}", headers=auth(user))
    cancelled = await client.get(f"{API}?status=cancelled", headers=auth(user))
    assert history.status_code == 200
    assert {item["id"] for item in history.json()["items"]} == {sent.id, pending.id}
    assert [item["id"] for item in cancelled.json()["items"]] == [pending.id]


async def test_the_cancelled_filter_never_leaks_another_users_notifications(
    client: AsyncClient,
    db_session: AsyncSession,
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    """S9: `status=cancelled` respeta ownership (ni por listado ni por `alert_id` ajeno)."""
    owner = await make_user()
    intruder = await make_user()
    owner_alert = await make_alert(await make_field(owner), event_type=EventType.FROST)
    other_alert = await make_alert(await make_field(intruder), event_type=EventType.RAIN)
    own = await _add_notification(db_session, owner_alert, status=NotificationStatus.CANCELLED)
    await _add_notification(db_session, other_alert, status=NotificationStatus.CANCELLED)

    own_page = await client.get(f"{API}?status=cancelled", headers=auth(owner))
    intruder_page = await client.get(f"{API}?status=cancelled", headers=auth(intruder))
    cross_filter = await client.get(
        f"{API}?status=cancelled&alert_id={owner_alert.id}", headers=auth(intruder)
    )

    assert own_page.status_code == 200
    assert [item["id"] for item in own_page.json()["items"]] == [own.id]
    assert all(item["status"] == "cancelled" for item in own_page.json()["items"])
    assert intruder_page.status_code == 200
    assert [item["alert_id"] for item in intruder_page.json()["items"]] == [other_alert.id]
    assert cross_filter.status_code == 200
    assert cross_filter.json()["items"] == []


async def test_filters_by_alert_id(
    client: AsyncClient,
    db_session: AsyncSession,
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    user = await make_user()
    field = await make_field(user)
    first_alert = await make_alert(field, event_type=EventType.FROST)
    second_alert = await make_alert(field, event_type=EventType.RAIN)
    expected = await _add_notification(db_session, first_alert)
    await _add_notification(db_session, second_alert)

    response = await client.get(f"{API}?alert_id={first_alert.id}", headers=auth(user))

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [expected.id]


async def test_combines_both_filters(
    client: AsyncClient,
    db_session: AsyncSession,
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    user = await make_user()
    field = await make_field(user)
    first_alert = await make_alert(field, event_type=EventType.FROST)
    second_alert = await make_alert(field, event_type=EventType.RAIN)
    expected = await _add_notification(db_session, first_alert, status=NotificationStatus.SENT)
    await _add_notification(db_session, first_alert, forecast_date=date(2026, 11, 19))
    await _add_notification(db_session, second_alert, status=NotificationStatus.SENT)

    response = await client.get(f"{API}?status=sent&alert_id={first_alert.id}", headers=auth(user))

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [expected.id]


async def test_filtering_by_another_users_alert_returns_an_empty_page(
    client: AsyncClient,
    db_session: AsyncSession,
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    owner = await make_user()
    intruder = await make_user()
    alert = await make_alert(await make_field(owner), event_type=EventType.FROST)
    await _add_notification(db_session, alert)

    response = await client.get(f"{API}?alert_id={alert.id}", headers=auth(intruder))

    assert response.status_code == 200
    assert response.json()["items"] == []


async def test_orders_by_created_at_desc_then_id_desc(
    client: AsyncClient,
    db_session: AsyncSession,
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    user = await make_user()
    alert = await make_alert(await make_field(user), event_type=EventType.FROST)
    oldest = await _add_notification(db_session, alert, forecast_date=date(2026, 11, 18))
    newest = await _add_notification(db_session, alert, forecast_date=date(2026, 11, 19))
    tied = await _add_notification(db_session, alert, forecast_date=date(2026, 11, 20))
    # `created_at` tiene server_default: se fuerza para probar el orden por fecha.
    await db_session.execute(
        update(Notification)
        .where(Notification.id == oldest.id)
        .values(created_at=datetime(2026, 11, 1, tzinfo=UTC))
    )
    await db_session.execute(
        update(Notification)
        .where(Notification.id == newest.id)
        .values(created_at=datetime(2026, 11, 3, tzinfo=UTC))
    )
    await db_session.execute(
        update(Notification)
        .where(Notification.id == tied.id)
        .values(created_at=datetime(2026, 11, 1, tzinfo=UTC))
    )

    response = await client.get(API, headers=auth(user))

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [
        newest.id,
        tied.id,
        oldest.id,
    ]


async def test_is_paginated(
    client: AsyncClient,
    db_session: AsyncSession,
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    user = await make_user()
    alert = await make_alert(await make_field(user), event_type=EventType.FROST)
    notifications = [
        await _add_notification(db_session, alert, forecast_date=FORECAST_DATE + timedelta(days=i))
        for i in range(3)
    ]

    response = await client.get(f"{API}?limit=1&offset=1", headers=auth(user))

    assert response.status_code == 200
    body = response.json()
    assert body["limit"] == 1
    assert body["offset"] == 1
    assert [item["id"] for item in body["items"]] == [notifications[1].id]


async def test_consecutive_pages_cover_every_row_without_repeating(
    client: AsyncClient,
    db_session: AsyncSession,
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    """Con `created_at` iguales (mismo `now()` de la transacción), el desempate por id es estable.

    Así la paginación por offset no repite ni saltea filas.
    """
    user = await make_user()
    alert = await make_alert(await make_field(user), event_type=EventType.FROST)
    oldest, middle, newest = [
        await _add_notification(db_session, alert, forecast_date=FORECAST_DATE + timedelta(days=i))
        for i in range(3)
    ]

    first_page = await client.get(f"{API}?limit=2&offset=0", headers=auth(user))
    second_page = await client.get(f"{API}?limit=2&offset=2", headers=auth(user))

    assert first_page.status_code == second_page.status_code == 200
    ids = [item["id"] for item in first_page.json()["items"]]
    ids += [item["id"] for item in second_page.json()["items"]]
    assert ids == [newest.id, middle.id, oldest.id]


async def test_an_offset_past_the_last_row_returns_an_empty_page(
    client: AsyncClient,
    db_session: AsyncSession,
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    user = await make_user()
    alert = await make_alert(await make_field(user), event_type=EventType.FROST)
    await _add_notification(db_session, alert)

    response = await client.get(f"{API}?offset=10", headers=auth(user))

    assert response.status_code == 200
    assert response.json() == {"items": [], "limit": 50, "offset": 10}


async def test_listing_with_an_out_of_range_alert_id_returns_422_not_500(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """S12c: el tope técnico de BIGINT da 422 (no deja que el driver falle con 500)."""
    user = await make_user()

    response = await client.get(f"{API}?alert_id={BIGINT_MAX + 1}", headers=auth(user))

    assert response.status_code == 422
    assert response.json()["detail"][0]["msg"] == f"Value error, must be at most {BIGINT_MAX}"


async def test_listing_with_the_biggest_alert_id_returns_an_empty_page(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """S12c: el mayor id válido de BIGINT no explota; no existe y devuelve lista vacía."""
    user = await make_user()

    response = await client.get(f"{API}?alert_id={BIGINT_MAX}", headers=auth(user))

    assert response.status_code == 200
    assert response.json() == {"items": [], "limit": 50, "offset": 0}


async def test_an_empty_histogram_returns_the_default_page(
    client: AsyncClient, make_user: MakeUser
) -> None:
    user = await make_user()

    response = await client.get(API, headers=auth(user))

    assert response.status_code == 200
    assert response.json() == {"items": [], "limit": 50, "offset": 0}


async def test_read_exposes_only_the_documented_fields(
    client: AsyncClient,
    db_session: AsyncSession,
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    """`last_error` y `next_attempt_at` son internos: nunca se serializan."""
    user = await make_user()
    alert = await make_alert(await make_field(user), event_type=EventType.HEAT_WAVE, threshold=40)
    notification = await _add_notification(
        db_session, alert, status=NotificationStatus.FAILED, probability=40
    )
    notification.last_error = "NotifierError: boom"
    await db_session.flush()

    response = await client.get(API, headers=auth(user))

    assert response.status_code == 200
    [item] = response.json()["items"]
    assert set(item) == {
        "id",
        "alert_id",
        "forecast_date",
        "event_type",
        "probability",
        "threshold",
        "status",
        "attempts",
        "sent_at",
        "created_at",
    }
    assert item["id"] == notification.id
    assert item["event_type"] == "heat_wave"
    assert item["status"] == "failed"
    assert item["sent_at"] is None


async def test_a_sent_notification_exposes_sent_at(
    client: AsyncClient,
    db_session: AsyncSession,
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    user = await make_user()
    alert = await make_alert(await make_field(user), event_type=EventType.FROST)
    notification = await _add_notification(db_session, alert, status=NotificationStatus.SENT)
    sent_at = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    notification.sent_at = sent_at
    await db_session.flush()

    response = await client.get(API, headers=auth(user))

    assert response.status_code == 200
    assert response.json()["items"][0]["sent_at"] == "2026-11-16T12:00:00Z"


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=101",
        "limit=1.5",
        "offset=-1",
        f"offset={BIGINT_MAX + 1}",
        "offset=99999999999999999999",
        "limit=abc",
        "status=tornado",
        "status=SENT",
        "status=",
        "alert_id=0",
        "alert_id=-1",
        f"alert_id={BIGINT_MAX + 1}",
        "alert_id=99999999999999999999",
        "alert_id=abc",
    ],
)
async def test_invalid_query_parameters_return_422(
    client: AsyncClient, make_user: MakeUser, query: str
) -> None:
    user = await make_user()

    response = await client.get(f"{API}?{query}", headers=auth(user))

    assert response.status_code == 422


async def test_requires_the_auth_header(client: AsyncClient) -> None:
    response = await client.get(API)

    assert response.status_code == 401
    assert response.json() == {"detail": "Missing or invalid X-User-Id header"}


async def test_a_deleted_users_header_returns_401(
    client: AsyncClient, db_session: AsyncSession, make_user: MakeUser
) -> None:
    user = await make_user()
    user_id = user.id

    await db_session.execute(delete(User).where(User.id == user_id))
    await db_session.flush()

    response = await client.get(API, headers={"X-User-Id": str(user_id)})

    assert response.status_code == 401


async def test_logging_notifier_masks_the_phone(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """El notifier por defecto nunca loguea el teléfono completo (RNF-08)."""
    notifier = LoggingNotifier()

    with caplog.at_level(logging.INFO, logger="app.notifiers.log"):
        await notifier.send(NotificationMessage(7, "+5491122334455", "hola"))

    [record] = caplog.records
    message = record.getMessage()
    assert "id=7" in message
    assert "phone=***4455" in message
    assert "+5491122334455" not in message
    assert "text='hola'" in message
