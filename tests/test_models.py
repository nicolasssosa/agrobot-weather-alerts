"""Tests de constraints del modelo de datos (docs/DATA_MODEL.md)."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Alert, Field, Notification, User, WeatherForecast
from app.models.enums import EventType, NotificationStatus
from tests.conftest import MakeAlert, MakeField, MakeForecast, MakeUser, auth

DAY = date(2026, 11, 20)


async def _make_notification(
    session: AsyncSession,
    alert: Alert,
    *,
    forecast_date: date = DAY,
    probability: int = 80,
) -> Notification:
    notification = Notification(
        alert_id=alert.id,
        forecast_date=forecast_date,
        event_type=alert.event_type,
        probability=probability,
        threshold=alert.threshold,
    )
    session.add(notification)
    await session.flush()
    return notification


async def _count(
    session: AsyncSession,
    model: type[Field] | type[WeatherForecast] | type[Alert] | type[Notification],
) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def test_probability_above_100_is_rejected(
    db_session: AsyncSession,
    make_field: MakeField,
    make_forecast: MakeForecast,
) -> None:
    field = await make_field()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await make_forecast(
                field, forecast_date=DAY, event_type=EventType.FROST, probability=101
            )


async def test_probability_below_0_is_rejected(
    db_session: AsyncSession,
    make_field: MakeField,
    make_forecast: MakeForecast,
) -> None:
    field = await make_field()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await make_forecast(
                field, forecast_date=DAY, event_type=EventType.FROST, probability=-1
            )


async def test_latitude_out_of_range_is_rejected(
    db_session: AsyncSession, make_field: MakeField
) -> None:
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await make_field(latitude=Decimal("91"))


async def test_duplicate_phone_number_is_rejected(
    db_session: AsyncSession, make_user: MakeUser
) -> None:
    user = await make_user()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await make_user(phone_number=user.phone_number)


async def test_duplicate_alert_for_same_field_event_and_threshold_is_rejected(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    field = await make_field()
    await make_alert(field, event_type=EventType.RAIN, threshold=60)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await make_alert(field, event_type=EventType.RAIN, threshold=60)


async def test_alerts_with_different_thresholds_for_same_field_and_event_are_allowed(
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    """D5/S9: varias alertas por (campo, evento) con umbrales distintos (50 y 80)."""
    field = await make_field()
    first = await make_alert(field, event_type=EventType.RAIN, threshold=60)
    second = await make_alert(field, event_type=EventType.RAIN, threshold=90)

    assert first.id != second.id


async def test_a_soft_deleted_alert_does_not_block_the_same_threshold(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    field = await make_field()
    deleted = await make_alert(field, event_type=EventType.RAIN, threshold=60)
    deleted.deleted_at = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    await db_session.flush()

    recreated = await make_alert(field, event_type=EventType.RAIN, threshold=60)

    assert recreated.id != deleted.id


async def test_threshold_out_of_range_is_rejected(
    db_session: AsyncSession, make_field: MakeField, make_alert: MakeAlert
) -> None:
    field = await make_field()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await make_alert(field, threshold=0)


async def test_duplicate_notification_for_same_alert_and_day_is_rejected(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    alert = await make_alert(await make_field())
    await _make_notification(db_session, alert)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await _make_notification(db_session, alert)


async def test_deleting_field_cascades_to_forecasts_alerts_and_notifications(
    db_session: AsyncSession,
    make_user: MakeUser,
    make_field: MakeField,
    make_forecast: MakeForecast,
    make_alert: MakeAlert,
) -> None:
    user = await make_user()
    field = await make_field(user=user)
    other_field = await make_field(user=user)
    await make_forecast(field, forecast_date=DAY, event_type=EventType.FROST, probability=80)
    alert = await make_alert(field, event_type=EventType.FROST)
    await _make_notification(db_session, alert)
    other_field_alert = await make_alert(other_field, event_type=EventType.RAIN, threshold=50)

    await db_session.execute(delete(Field).where(Field.id == field.id))

    assert await _count(db_session, WeatherForecast) == 0
    assert await _count(db_session, Alert) == 1
    assert await _count(db_session, Notification) == 0
    remaining_alert = (
        await db_session.execute(select(Alert).where(Alert.id == other_field_alert.id))
    ).scalar_one()
    assert remaining_alert.field_id == other_field.id
    assert (await db_session.execute(select(User).where(User.id == user.id))).scalar_one()


async def test_event_type_is_stored_as_value(
    db_session: AsyncSession,
    make_field: MakeField,
    make_forecast: MakeForecast,
) -> None:
    field = await make_field()
    await make_forecast(field, forecast_date=DAY, event_type=EventType.STRONG_WIND, probability=50)
    raw = (
        await db_session.execute(
            text("SELECT event_type FROM weather_forecasts WHERE field_id = :field_id"),
            {"field_id": field.id},
        )
    ).scalar_one()
    assert raw == "strong_wind"


async def test_notification_defaults(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    alert = await make_alert(await make_field())
    notification = await _make_notification(db_session, alert)
    await db_session.refresh(notification)

    assert notification.status is NotificationStatus.PENDING
    assert notification.attempts == 0
    assert notification.next_attempt_at is not None
    assert notification.sent_at is None
    assert notification.last_error is None


async def test_threshold_above_100_is_rejected(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    field = await make_field()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await make_alert(field, threshold=101)


async def test_threshold_and_probability_boundaries_are_accepted(
    db_session: AsyncSession,
    make_field: MakeField,
    make_forecast: MakeForecast,
    make_alert: MakeAlert,
) -> None:
    field = await make_field()
    await make_alert(field, event_type=EventType.FROST, threshold=1)
    await make_alert(field, event_type=EventType.RAIN, threshold=100)
    await make_forecast(field, forecast_date=DAY, event_type=EventType.HAIL, probability=0)
    await make_forecast(field, forecast_date=DAY, event_type=EventType.HEAT_WAVE, probability=100)
    notification = await _make_notification(
        db_session,
        await make_alert(field, event_type=EventType.STRONG_WIND, threshold=1),
        probability=0,
    )
    assert notification.id is not None


async def test_duplicate_field_name_for_same_user_is_rejected(
    db_session: AsyncSession,
    make_user: MakeUser,
    make_field: MakeField,
) -> None:
    user = await make_user()
    await make_field(user=user, name="El Lote")
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await make_field(user=user, name="El Lote")


async def test_same_field_name_for_another_user_is_allowed(make_field: MakeField) -> None:
    first = await make_field(name="El Lote")
    second = await make_field(name="El Lote")
    assert first.id != second.id
    assert first.user_id != second.user_id


async def test_longitude_out_of_range_is_rejected(
    db_session: AsyncSession, make_field: MakeField
) -> None:
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await make_field(longitude=Decimal("181"))


async def test_latitude_and_longitude_boundaries_are_accepted(make_field: MakeField) -> None:
    north_east = await make_field(latitude=Decimal("90"), longitude=Decimal("180"))
    south_west = await make_field(latitude=Decimal("-90"), longitude=Decimal("-180"))
    assert north_east.id is not None
    assert south_west.id is not None


@pytest.mark.parametrize(
    "sql",
    [
        (
            "INSERT INTO weather_forecasts"
            " (field_id, forecast_date, event_type, probability, source, issued_at, fetched_at)"
            " VALUES (:field_id, :day, 'tornado', 50, 'mock', now(), now())"
        ),
        "INSERT INTO alerts (field_id, event_type, threshold) VALUES (:field_id, 'tornado', 50)",
        (
            "INSERT INTO notifications"
            " (alert_id, forecast_date, event_type, probability, threshold)"
            " VALUES (:alert_id, :day, 'tornado', 50, 50)"
        ),
    ],
    ids=["weather_forecasts", "alerts", "notifications"],
)
async def test_invalid_event_type_is_rejected_by_database(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    sql: str,
) -> None:
    field = await make_field()
    alert = await make_alert(field, event_type=EventType.RAIN)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                text(sql),
                {"field_id": field.id, "alert_id": alert.id, "day": DAY},
            )


async def test_duplicate_forecast_for_same_field_day_and_event_is_rejected(
    db_session: AsyncSession,
    make_field: MakeField,
    make_forecast: MakeForecast,
) -> None:
    field = await make_field()
    await make_forecast(field, forecast_date=DAY, event_type=EventType.FROST, probability=80)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await make_forecast(
                field, forecast_date=DAY, event_type=EventType.FROST, probability=90
            )


async def test_forecast_for_another_event_on_the_same_day_is_allowed(
    make_field: MakeField,
    make_forecast: MakeForecast,
) -> None:
    field = await make_field()
    frost = await make_forecast(
        field, forecast_date=DAY, event_type=EventType.FROST, probability=80
    )
    rain = await make_forecast(field, forecast_date=DAY, event_type=EventType.RAIN, probability=60)
    assert frost.id != rain.id


async def test_notification_for_same_alert_on_another_day_is_allowed(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    alert = await make_alert(await make_field())
    first = await _make_notification(db_session, alert, forecast_date=DAY)
    second = await _make_notification(db_session, alert, forecast_date=date(2026, 11, 21))
    assert first.id != second.id


@pytest.mark.parametrize("probability", [-1, 101])
async def test_notification_probability_out_of_range_is_rejected(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    probability: int,
) -> None:
    alert = await make_alert(await make_field())
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await _make_notification(db_session, alert, probability=probability)


@pytest.mark.parametrize("threshold", [0, 101])
async def test_notification_threshold_out_of_range_is_rejected(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    threshold: int,
) -> None:
    alert = await make_alert(await make_field())
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "INSERT INTO notifications"
                    " (alert_id, forecast_date, event_type, probability, threshold)"
                    " VALUES (:alert_id, :day, 'frost', 50, :threshold)"
                ),
                {"alert_id": alert.id, "day": DAY, "threshold": threshold},
            )


async def test_negative_attempts_is_rejected(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    alert = await make_alert(await make_field())
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "INSERT INTO notifications"
                    " (alert_id, forecast_date, event_type, probability, threshold, attempts)"
                    " VALUES (:alert_id, :day, 'frost', 50, 50, -1)"
                ),
                {"alert_id": alert.id, "day": DAY},
            )


async def test_invalid_notification_status_is_rejected(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    alert = await make_alert(await make_field())
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "INSERT INTO notifications"
                    " (alert_id, forecast_date, event_type, probability, threshold, status)"
                    " VALUES (:alert_id, :day, 'frost', 50, 50, 'bogus')"
                ),
                {"alert_id": alert.id, "day": DAY},
            )


async def test_cancelled_is_a_valid_notification_status_in_database(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    """S9: `cancelled` entra en el CHECK de `notifications.status`."""
    alert = await make_alert(await make_field())
    await db_session.execute(
        text(
            "INSERT INTO notifications"
            " (alert_id, forecast_date, event_type, probability, threshold, status)"
            " VALUES (:alert_id, :day, 'frost', 50, 50, 'cancelled')"
        ),
        {"alert_id": alert.id, "day": DAY},
    )

    status = (
        await db_session.execute(
            text("SELECT status FROM notifications WHERE alert_id = :alert_id"),
            {"alert_id": alert.id},
        )
    ).scalar_one()
    assert status == "cancelled"


async def test_deleting_user_cascades_to_fields_and_descendants(
    db_session: AsyncSession,
    make_user: MakeUser,
    make_field: MakeField,
    make_forecast: MakeForecast,
    make_alert: MakeAlert,
) -> None:
    user = await make_user()
    other_user = await make_user()
    field = await make_field(user=user)
    await make_forecast(field, forecast_date=DAY, event_type=EventType.FROST, probability=80)
    alert = await make_alert(field, event_type=EventType.FROST)
    await _make_notification(db_session, alert)
    kept_field = await make_field(user=other_user)
    kept_alert = await make_alert(kept_field, event_type=EventType.RAIN, threshold=50)

    await db_session.execute(delete(User).where(User.id == user.id))

    assert await _count(db_session, Field) == 1
    assert await _count(db_session, WeatherForecast) == 0
    assert await _count(db_session, Alert) == 1
    assert await _count(db_session, Notification) == 0
    assert (await db_session.execute(select(Field).where(Field.id == kept_field.id))).scalar_one()
    assert (await db_session.execute(select(Alert).where(Alert.id == kept_alert.id))).scalar_one()


async def test_deleting_alert_cascades_to_its_notifications(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    field = await make_field()
    alert = await make_alert(field, event_type=EventType.FROST)
    await _make_notification(db_session, alert)
    kept_alert = await make_alert(field, event_type=EventType.RAIN, threshold=50)
    await _make_notification(db_session, kept_alert, forecast_date=date(2026, 11, 21))

    await db_session.execute(delete(Alert).where(Alert.id == alert.id))

    assert await _count(db_session, Notification) == 1
    assert (await db_session.execute(select(Field).where(Field.id == field.id))).scalar_one()


async def test_alert_with_unknown_field_is_rejected(db_session: AsyncSession) -> None:
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "INSERT INTO alerts (field_id, event_type, threshold)"
                    " VALUES (:field_id, 'frost', 50)"
                ),
                {"field_id": 10**12},
            )


async def test_alert_is_active_defaults_to_true_in_database(
    db_session: AsyncSession, make_field: MakeField
) -> None:
    field = await make_field()
    alert_id = (
        await db_session.execute(
            text(
                "INSERT INTO alerts (field_id, event_type, threshold)"
                " VALUES (:field_id, 'frost', 50) RETURNING id"
            ),
            {"field_id": field.id},
        )
    ).scalar_one()
    is_active = (
        await db_session.execute(
            text("SELECT is_active FROM alerts WHERE id = :alert_id"), {"alert_id": alert_id}
        )
    ).scalar_one()
    assert is_active is True


async def test_notification_defaults_are_applied_by_database(
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    alert = await make_alert(await make_field(), threshold=60)
    notification_id = (
        await db_session.execute(
            text(
                "INSERT INTO notifications"
                " (alert_id, forecast_date, event_type, probability, threshold)"
                " VALUES (:alert_id, :day, 'frost', 80, 60) RETURNING id"
            ),
            {"alert_id": alert.id, "day": DAY},
        )
    ).scalar_one()
    row = (
        await db_session.execute(
            text(
                "SELECT status, attempts, next_attempt_at, last_error, sent_at"
                " FROM notifications WHERE id = :notification_id"
            ),
            {"notification_id": notification_id},
        )
    ).one()
    assert row.status == "pending"
    assert row.attempts == 0
    assert row.next_attempt_at is not None
    assert row.last_error is None
    assert row.sent_at is None


async def test_auth_helper_builds_the_user_header(make_user: MakeUser) -> None:
    user = await make_user()
    assert auth(user) == {"X-User-Id": str(user.id)}


async def test_factories_flush_rows_with_unique_defaults(
    make_user: MakeUser,
    make_field: MakeField,
    make_forecast: MakeForecast,
    make_alert: MakeAlert,
) -> None:
    first = await make_user()
    second = await make_user()
    field = await make_field(user=first)
    forecast = await make_forecast(
        field, forecast_date=DAY, event_type=EventType.HAIL, probability=30
    )
    alert = await make_alert(field, event_type=EventType.HAIL, threshold=30)

    assert first.id is not None
    assert first.phone_number != second.phone_number
    assert field.id is not None
    assert field.user_id == first.id
    assert forecast.id is not None
    assert forecast.source == "mock"
    assert alert.id is not None
    assert alert.is_active is True


# --- Migración 0002: hardening (S2) -----------------------------------------


@pytest.mark.parametrize(
    "phone_number",
    [
        "5491122334455",  # sin `+`
        "+0123456789",  # código de país empieza con 0
        "+1234567",  # 7 dígitos: por debajo del mínimo E.164
        "+1234567890123456",  # 16 dígitos: por encima del máximo E.164
        "telefono",
    ],
)
async def test_phone_number_must_be_e164_in_the_database(
    db_session: AsyncSession, phone_number: str
) -> None:
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(User(phone_number=phone_number, name="Bad Phone"))
            await db_session.flush()


async def test_e164_boundary_phone_numbers_are_accepted(make_user: MakeUser) -> None:
    shortest = await make_user(phone_number="+12345678")  # 8 dígitos
    longest = await make_user(phone_number="+123456789012345")  # 15 dígitos

    assert shortest.id is not None
    assert longest.id is not None


async def test_last_error_of_1000_characters_is_accepted(
    db_session: AsyncSession, make_field: MakeField, make_alert: MakeAlert
) -> None:
    alert = await make_alert(await make_field())
    notification = await _make_notification(db_session, alert)
    notification.last_error = "x" * 1000
    await db_session.flush()

    assert notification.last_error is not None
    assert len(notification.last_error) == 1000


async def test_last_error_longer_than_1000_characters_is_rejected(
    db_session: AsyncSession, make_field: MakeField, make_alert: MakeAlert
) -> None:
    alert = await make_alert(await make_field())
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            notification = await _make_notification(db_session, alert)
            notification.last_error = "x" * 1001
            await db_session.flush()
