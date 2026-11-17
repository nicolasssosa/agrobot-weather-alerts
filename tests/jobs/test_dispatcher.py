"""Tests del dispatcher de notificaciones.

Ver docs/TESTING.md §`tests/jobs/test_dispatcher.py` y docs/JOBS.md §Dispatcher.
`now` siempre se inyecta (nunca el reloj real).
"""

import asyncio
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.core.pii import mask_phone, mask_phone_in_text
from app.jobs.dispatcher import (
    _USERS_CLAIM_SQL,
    ClaimedNotification,
    DispatchResult,
    _claim_batch,
    _claim_statement,
    _record_outcomes,
    _SendOutcome,
    build_digest_text,
    build_message_text,
    dispatch_pending,
)
from app.models import (
    Alert,
    EventType,
    Field,
    Notification,
    NotificationStatus,
    User,
    WeatherForecast,
)
from app.notifiers.base import NotificationMessage, NotifierError
from app.notifiers.log import LoggingNotifier
from app.services.alerts import delete_alert
from tests.conftest import (
    TEST_DATABASE_URL,
    FakeNotifier,
    MakeAlert,
    MakeField,
    MakeUser,
)

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
#: Claim con un lease en el futuro lejano: el `DELETE` de la API compara contra `now()` real,
#: así que la fila arrendada no debe parecer vencida por el reloj del test.
LEASE_SAFE_NOW = datetime(2099, 1, 1, tzinfo=UTC)
#: Día pronosticado **posterior** a `LEASE_SAFE_NOW`: con la validación de S13
#: (`forecast_date_passed`) un día de 2026 se cancelaría en vez de enviarse.
LEASE_SAFE_FORECAST_DATE = date(2099, 1, 3)
FORECAST_DATE = date(2026, 11, 18)

DISPATCH_KWARGS = {
    "batch_size": 10,
    "max_attempts": 3,
    "backoff_base_seconds": 30,
    "timeout_seconds": 1.0,
    "concurrency": 10,
    "lease_seconds": 60,
    "max_forecast_age_hours": 24,
    "digest_max_items": 10,
}


def _clock_at(moment: datetime) -> Callable[[], datetime]:
    """Reloj fijo: `sent_at` y el backoff usan una hora determinista."""
    return lambda: moment


async def _make_notification(
    session: AsyncSession,
    alert: Alert,
    *,
    forecast_date: date = FORECAST_DATE,
    status: NotificationStatus = NotificationStatus.PENDING,
    attempts: int = 0,
    next_attempt_at: datetime = NOW,
    sent_at: datetime | None = None,
    last_error: str | None = None,
    probability: int = 85,
    episode: int = 1,
    forecast_probability: int | None = None,
    forecast_issued_at: datetime | None = NOW,
) -> Notification:
    """Crea la notificación y, si corresponde, el pronóstico vigente que la respalda.

    `probability` es el snapshot de la notificación; `forecast_probability` (default: el mismo)
    es la probabilidad del pronóstico vigente, que manda en la re-validación de umbral (SEC-5).
    `forecast_issued_at=None` simula un pronóstico que ya no existe; otra hora simula uno
    emitido antes (vencido) o después (fresco) del corte. Los tests nunca usan el reloj real.
    """
    if forecast_issued_at is not None:
        existing_forecast = await session.scalar(
            select(WeatherForecast.id).where(
                WeatherForecast.field_id == alert.field_id,
                WeatherForecast.forecast_date == forecast_date,
                WeatherForecast.event_type == alert.event_type,
            )
        )
        if existing_forecast is None:
            session.add(
                WeatherForecast(
                    field_id=alert.field_id,
                    forecast_date=forecast_date,
                    event_type=alert.event_type,
                    probability=probability
                    if forecast_probability is None
                    else forecast_probability,
                    source="mock",
                    issued_at=forecast_issued_at,
                    fetched_at=NOW,
                )
            )
            await session.flush()
    notification = Notification(
        alert_id=alert.id,
        forecast_date=forecast_date,
        event_type=alert.event_type,
        probability=probability,
        threshold=alert.threshold,
        episode=episode,
        status=status,
        attempts=attempts,
        next_attempt_at=next_attempt_at,
        sent_at=sent_at,
        last_error=last_error,
    )
    session.add(notification)
    await session.flush()
    return notification


def _compiled_claim_sql() -> str:
    return str(
        _claim_statement(NOW, user_ids=[1], digest_max_items=10).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


# --- Texto del mensaje ------------------------------------------------------


def test_build_message_text_matches_jobs_md_exactly() -> None:
    text = build_message_text(
        event_type=EventType.FROST,
        field_name="Lote Norte",
        probability=85,
        threshold=70,
        forecast_date=date(2026, 11, 18),
    )

    assert text == (
        '⚠️ Alerta de helada en "Lote Norte": 85% de probabilidad '
        "para el 18/11/2026 (tu umbral: 70%)."
    )


@pytest.mark.parametrize(
    ("event_type", "label"),
    [
        (EventType.FROST, "helada"),
        (EventType.RAIN, "lluvia"),
        (EventType.HAIL, "granizo"),
        (EventType.STRONG_WIND, "viento fuerte"),
        (EventType.HEAT_WAVE, "ola de calor"),
    ],
)
def test_build_message_text_uses_the_spanish_label(event_type: EventType, label: str) -> None:
    text = build_message_text(
        event_type=event_type,
        field_name="Lote",
        probability=1,
        threshold=1,
        forecast_date=date(2027, 1, 2),
    )

    assert label in text
    assert "02/01/2027" in text


# --- Claim ----------------------------------------------------------------


def test_claim_locks_only_notifications_and_orders_by_due_date() -> None:
    sql = _compiled_claim_sql()

    assert "FOR UPDATE OF notifications SKIP LOCKED" in sql
    assert "notifications.status = 'pending'" in sql
    assert "ORDER BY notifications.next_attempt_at, notifications.id" in sql
    # S13: el tope es por usuario (ventana sobre `fields.user_id`), no por lote.
    assert "row_number() OVER (PARTITION BY fields.user_id" in sql


def test_the_batch_claim_locks_the_users_with_for_no_key_update() -> None:
    """S13: el lock de la fila del usuario garantiza un solo mensaje por usuario."""
    sql = str(_USERS_CLAIM_SQL)

    assert "FOR NO KEY UPDATE SKIP LOCKED" in sql
    assert "min(n.next_attempt_at), f.user_id" in sql
    assert "LIMIT :batch_size" in sql


# --- Éxito ----------------------------------------------------------------


async def test_pending_notification_is_sent_with_the_exact_message(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    owner = await make_user(phone_number="+5491100000001")
    field = await make_field(owner, name="Lote Norte")
    alert = await make_alert(field, event_type=EventType.FROST, threshold=70)
    notification = await _make_notification(db_session, alert, probability=85)

    result = await dispatch_pending(
        test_sessionmaker, FakeNotifier(), now=NOW, clock=_clock_at(NOW), **DISPATCH_KWARGS
    )

    assert result == DispatchResult(sent=1, retried=0, failed=0, users=1)
    await db_session.refresh(notification)
    assert notification.status == NotificationStatus.SENT
    assert notification.sent_at == NOW
    assert notification.attempts == 1
    assert notification.last_error is None


async def test_the_notifier_receives_the_owner_phone_and_the_exact_text(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    owner = await make_user(phone_number="+5491100000002")
    field = await make_field(owner, name="La Esperanza")
    alert = await make_alert(field, event_type=EventType.RAIN, threshold=80)
    notification = await _make_notification(db_session, alert, probability=95)
    notifier = FakeNotifier()

    await dispatch_pending(test_sessionmaker, notifier, now=NOW, **DISPATCH_KWARGS)

    assert notifier.sent == [
        NotificationMessage(
            notification_id=notification.id,
            phone_number="+5491100000002",
            text=(
                '⚠️ Alerta de lluvia en "La Esperanza": 95% de probabilidad '
                "para el 18/11/2026 (tu umbral: 80%)."
            ),
        )
    ]


async def test_the_claim_lease_blocks_other_dispatchers_until_it_expires(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    """El claim arrienda las filas: otro worker no las ve hasta que vence el lease.

    Simula un worker que muere entre el claim y el envío (shutdown): la fila queda `pending`
    con `next_attempt_at = now + lease` y se reintenta sola cuando el lease vence.
    """
    alert = await make_alert(await make_field())
    notification = await _make_notification(db_session, alert)

    with pytest.raises(asyncio.CancelledError):
        await dispatch_pending(test_sessionmaker, _CancellingNotifier(), now=NOW, **DISPATCH_KWARGS)

    await db_session.refresh(notification)
    assert notification.status == NotificationStatus.PENDING
    assert notification.attempts == 0
    lease_seconds = DISPATCH_KWARGS["lease_seconds"]
    assert notification.next_attempt_at == NOW + timedelta(seconds=lease_seconds)

    notifier = FakeNotifier()
    while_leased = await dispatch_pending(test_sessionmaker, notifier, now=NOW, **DISPATCH_KWARGS)
    assert while_leased == DispatchResult(0, 0, 0)
    assert notifier.calls == 0

    later = NOW + timedelta(seconds=lease_seconds + 1)
    result = await dispatch_pending(test_sessionmaker, notifier, now=later, **DISPATCH_KWARGS)
    assert result == DispatchResult(sent=1, retried=0, failed=0, users=1)
    await db_session.refresh(notification)
    assert notification.status == NotificationStatus.SENT
    assert notification.attempts == 1


# --- Reintentos y backoff --------------------------------------------------


async def test_failure_keeps_it_pending_and_schedules_the_backoff(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    field = await make_field()
    alert = await make_alert(field)
    notification = await _make_notification(db_session, alert)

    result = await dispatch_pending(
        test_sessionmaker,
        FakeNotifier(fail_times=1),
        now=NOW,
        clock=_clock_at(NOW),
        **DISPATCH_KWARGS,
    )

    assert result == DispatchResult(sent=0, retried=1, failed=0, users=1)
    await db_session.refresh(notification)
    assert notification.status == NotificationStatus.PENDING
    assert notification.attempts == 1
    assert notification.last_error == "NotifierError: simulated failure"
    assert notification.next_attempt_at == NOW + timedelta(seconds=30)
    assert notification.sent_at is None


async def test_a_retry_is_sent_once_its_backoff_has_passed(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    field = await make_field()
    alert = await make_alert(field)
    notification = await _make_notification(db_session, alert)
    notifier = FakeNotifier(fail_times=1)

    await dispatch_pending(
        test_sessionmaker, notifier, now=NOW, clock=_clock_at(NOW), **DISPATCH_KWARGS
    )

    # Todavía no venció: no se toca.
    too_early = await dispatch_pending(
        test_sessionmaker, notifier, now=NOW, clock=_clock_at(NOW), **DISPATCH_KWARGS
    )
    assert too_early == DispatchResult(0, 0, 0)
    assert notifier.calls == 1

    later = NOW + timedelta(seconds=30)
    result = await dispatch_pending(
        test_sessionmaker, notifier, now=later, clock=_clock_at(later), **DISPATCH_KWARGS
    )

    assert result == DispatchResult(sent=1, retried=0, failed=0, users=1)
    await db_session.refresh(notification)
    assert notification.status == NotificationStatus.SENT
    assert notification.attempts == 2
    assert notification.sent_at == later
    assert notification.last_error is None


async def test_backoff_doubles_each_attempt(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    field = await make_field()
    alert = await make_alert(field)
    notification = await _make_notification(db_session, alert)
    notifier = FakeNotifier(fail_times=10)
    kwargs = {**DISPATCH_KWARGS, "max_attempts": 5}

    expected_delays = [30, 60, 120, 240]
    now = NOW
    for attempt, delay in enumerate(expected_delays, start=1):
        await dispatch_pending(test_sessionmaker, notifier, now=now, clock=_clock_at(now), **kwargs)
        await db_session.refresh(notification)
        assert notification.attempts == attempt
        assert notification.next_attempt_at == now + timedelta(seconds=delay)
        now = notification.next_attempt_at

    await dispatch_pending(test_sessionmaker, notifier, now=now, clock=_clock_at(now), **kwargs)
    await db_session.refresh(notification)
    assert notification.attempts == 5
    assert notification.status == NotificationStatus.FAILED
    # `failed` no se reclama más: queda solo el lease puesto por el claim.
    assert notification.next_attempt_at == now + timedelta(seconds=DISPATCH_KWARGS["lease_seconds"])


async def test_exhausting_max_attempts_marks_it_failed(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    field = await make_field()
    alert = await make_alert(field)
    notification = await _make_notification(db_session, alert, attempts=2)

    result = await dispatch_pending(
        test_sessionmaker, FakeNotifier(fail_times=1), now=NOW, **DISPATCH_KWARGS
    )

    assert result == DispatchResult(sent=0, retried=0, failed=1, users=1)
    await db_session.refresh(notification)
    assert notification.status == NotificationStatus.FAILED
    assert notification.attempts == 3
    assert notification.last_error == "NotifierError: simulated failure"
    assert notification.next_attempt_at == NOW + timedelta(seconds=DISPATCH_KWARGS["lease_seconds"])


async def test_future_next_attempt_at_is_not_claimed(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    field = await make_field()
    alert = await make_alert(field)
    notification = await _make_notification(
        db_session, alert, next_attempt_at=NOW + timedelta(minutes=1)
    )
    notifier = FakeNotifier()

    result = await dispatch_pending(test_sessionmaker, notifier, now=NOW, **DISPATCH_KWARGS)

    assert result == DispatchResult(0, 0, 0)
    assert notifier.calls == 0
    await db_session.refresh(notification)
    assert notification.status == NotificationStatus.PENDING
    assert notification.attempts == 0


async def test_sent_and_failed_notifications_are_not_reclaimed(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    field = await make_field()
    alert = await make_alert(field)
    sent = await _make_notification(
        db_session, alert, status=NotificationStatus.SENT, attempts=1, sent_at=NOW
    )
    failed = await _make_notification(
        db_session, alert, forecast_date=date(2026, 11, 19), status=NotificationStatus.FAILED
    )
    notifier = FakeNotifier()

    result = await dispatch_pending(test_sessionmaker, notifier, now=NOW, **DISPATCH_KWARGS)

    assert result == DispatchResult(0, 0, 0)
    assert notifier.calls == 0
    for notification, expected in (
        (sent, NotificationStatus.SENT),
        (failed, NotificationStatus.FAILED),
    ):
        await db_session.refresh(notification)
        assert notification.status == expected


# --- Timeout y cancelación -------------------------------------------------


class _SlowNotifier:
    """Notifier que tarda más que el timeout del dispatcher."""

    async def send(self, message: NotificationMessage) -> None:
        await asyncio.sleep(1)


async def test_a_slow_notifier_times_out_and_counts_as_a_failure(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    field = await make_field()
    alert = await make_alert(field)
    notification = await _make_notification(db_session, alert)

    result = await dispatch_pending(
        test_sessionmaker, _SlowNotifier(), now=NOW, **{**DISPATCH_KWARGS, "timeout_seconds": 0.05}
    )

    assert result == DispatchResult(sent=0, retried=1, failed=0, users=1)
    await db_session.refresh(notification)
    assert notification.attempts == 1
    assert notification.last_error is not None
    assert notification.last_error.startswith("TimeoutError")


class _CancellingNotifier:
    """Notifier que se cancela a mitad del envío (p. ej. shutdown del worker)."""

    async def send(self, message: NotificationMessage) -> None:
        raise asyncio.CancelledError


async def test_cancelled_error_propagates_and_changes_nothing(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    field = await make_field()
    alert = await make_alert(field)
    notification = await _make_notification(db_session, alert)

    with pytest.raises(asyncio.CancelledError):
        await dispatch_pending(test_sessionmaker, _CancellingNotifier(), now=NOW, **DISPATCH_KWARGS)

    await db_session.refresh(notification)
    assert notification.status == NotificationStatus.PENDING
    assert notification.attempts == 0
    assert notification.last_error is None


# --- PII y longitud del error ---------------------------------------------


class _LeakyNotifier:
    """Notifier que incluye el teléfono del destinatario en el error."""

    async def send(self, message: NotificationMessage) -> None:
        raise NotifierError(f"provider rejected {message.phone_number}")


class _FailingNotifier:
    """Notifier que siempre falla con el error indicado."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def send(self, message: NotificationMessage) -> None:
        raise self.error


async def test_the_phone_is_masked_in_last_error(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    owner = await make_user(phone_number="+5491122334455")
    field = await make_field(owner)
    alert = await make_alert(field)
    notification = await _make_notification(db_session, alert)

    await dispatch_pending(test_sessionmaker, _LeakyNotifier(), now=NOW, **DISPATCH_KWARGS)

    await db_session.refresh(notification)
    assert notification.last_error == "NotifierError: provider rejected ***4455"
    assert "+5491122334455" not in (notification.last_error or "")


async def test_a_huge_error_is_truncated_to_the_db_limit(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    field = await make_field()
    alert = await make_alert(field)
    notification = await _make_notification(db_session, alert)

    await dispatch_pending(
        test_sessionmaker, _FailingNotifier(NotifierError("x" * 5000)), now=NOW, **DISPATCH_KWARGS
    )

    await db_session.refresh(notification)
    assert notification.last_error is not None
    assert len(notification.last_error) == 1000
    assert notification.last_error == "NotifierError: " + "x" * (1000 - len("NotifierError: "))


# --- Lotes ----------------------------------------------------------------


async def test_batch_size_limits_the_users_per_batch(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    """S13: `batch_size` es la cantidad de usuarios (= mensajes) por lote."""
    notifications = []
    for _ in range(3):
        owner = await make_user()
        alert = await make_alert(await make_field(owner))
        notifications.append(await _make_notification(db_session, alert))
    notifier = FakeNotifier()

    result = await dispatch_pending(
        test_sessionmaker, notifier, now=NOW, **{**DISPATCH_KWARGS, "batch_size": 2}
    )

    assert result == DispatchResult(sent=2, retried=0, failed=0, users=2)
    assert [message.notification_id for message in notifier.sent] == [
        notifications[0].id,
        notifications[1].id,
    ]
    await db_session.refresh(notifications[2])
    assert notifications[2].status == NotificationStatus.PENDING


class _SelectiveNotifier:
    """Falla solo para los ids indicados; registra los envíos exitosos."""

    def __init__(self, *, failing_ids: set[int]) -> None:
        self.failing_ids = failing_ids
        self.sent: list[NotificationMessage] = []

    async def send(self, message: NotificationMessage) -> None:
        if message.notification_id in self.failing_ids:
            raise NotifierError("boom")
        self.sent.append(message)


async def test_a_mixed_batch_counts_success_retry_and_exhausted(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    """Un usuario por resultado: cada notificación lleva su propio `attempts`."""
    notifications = []
    for attempts in (0, 0, 2):
        owner = await make_user()
        alert = await make_alert(await make_field(owner))
        notifications.append(await _make_notification(db_session, alert, attempts=attempts))
    ok, retry, exhausted = notifications
    notifier = _SelectiveNotifier(failing_ids={retry.id, exhausted.id})

    result = await dispatch_pending(test_sessionmaker, notifier, now=NOW, **DISPATCH_KWARGS)

    assert result == DispatchResult(sent=1, retried=1, failed=1, users=3)
    assert [message.notification_id for message in notifier.sent] == [ok.id]
    statuses = {}
    for notification in (ok, retry, exhausted):
        await db_session.refresh(notification)
        statuses[notification.id] = notification.status
    assert statuses == {
        ok.id: NotificationStatus.SENT,
        retry.id: NotificationStatus.PENDING,
        exhausted.id: NotificationStatus.FAILED,
    }


# --- Plan de la query ------------------------------------------------------


async def test_planner_uses_the_partial_pending_index(
    db_session: AsyncSession, make_field: MakeField, make_alert: MakeAlert
) -> None:
    """Con `enable_seqscan = off`, el claim elige `ix_notifications_pending_due`.

    El índice es parcial (`WHERE status = 'pending'`), así que solo aplica si el predicado de
    estado es una igualdad.
    """
    field = await make_field()
    alert = await make_alert(field)
    for offset in range(20):
        await _make_notification(
            db_session,
            alert,
            forecast_date=FORECAST_DATE + timedelta(days=offset),
            status=NotificationStatus.SENT,
            attempts=1,
            sent_at=NOW,
        )
    await _make_notification(db_session, alert, forecast_date=FORECAST_DATE + timedelta(days=100))

    await db_session.execute(text("ANALYZE notifications"))
    await db_session.execute(text("SET LOCAL enable_seqscan = off"))
    plan_lines = (await db_session.execute(text("EXPLAIN " + _compiled_claim_sql()))).scalars()
    plan = "\n".join(plan_lines)

    assert "ix_notifications_pending_due" in plan


# --- Concurrencia ----------------------------------------------------------


async def _dispatch(
    session_factory: async_sessionmaker[AsyncSession], notifier: FakeNotifier
) -> DispatchResult:
    """Un despacho completo: el dispatcher maneja y commitea sus propias transacciones."""
    return await dispatch_pending(
        session_factory, notifier, now=NOW, clock=_clock_at(NOW), **DISPATCH_KWARGS
    )


async def test_two_concurrent_dispatchers_send_a_single_message_per_user() -> None:
    """Dos workers en conexiones distintas y datos commiteados: un solo mensaje por usuario.

    El claim lockea la fila del usuario (`FOR NO KEY UPDATE SKIP LOCKED`): la otra réplica la
    saltea. Además, ninguna notificación se envía dos veces.
    """
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    phone_number = f"+54911{uuid.uuid4().int % 10**8:08d}"
    try:
        async with session_factory() as setup:
            user = User(phone_number=phone_number, name=f"QA dispatcher {uuid.uuid4().hex}")
            setup.add(user)
            await setup.flush()
            field = Field(
                user_id=user.id,
                name=f"QA dispatcher field {uuid.uuid4().hex}",
                latitude=Decimal("-34.6"),
                longitude=Decimal("-58.4"),
            )
            setup.add(field)
            await setup.flush()
            alert = Alert(
                field_id=field.id, event_type=EventType.FROST, threshold=50, is_active=True
            )
            setup.add(alert)
            await setup.flush()
            setup.add_all(
                WeatherForecast(
                    field_id=field.id,
                    forecast_date=FORECAST_DATE + timedelta(days=offset),
                    event_type=EventType.FROST,
                    probability=80,
                    source="mock",
                    issued_at=NOW,
                    fetched_at=NOW,
                )
                for offset in range(6)
            )
            setup.add_all(
                Notification(
                    alert_id=alert.id,
                    forecast_date=FORECAST_DATE + timedelta(days=offset),
                    event_type=EventType.FROST,
                    probability=80,
                    threshold=50,
                    status=NotificationStatus.PENDING,
                    attempts=0,
                    next_attempt_at=NOW - timedelta(minutes=1),
                )
                for offset in range(6)
            )
            await setup.commit()

        first_notifier = FakeNotifier()
        second_notifier = FakeNotifier()
        first, second = await asyncio.gather(
            _dispatch(session_factory, first_notifier),
            _dispatch(session_factory, second_notifier),
        )

        messages = first_notifier.sent + second_notifier.sent
        # Un solo mensaje para el usuario, con sus 6 notificaciones.
        assert len(messages) == 1
        assert messages[0].text.startswith("⚠️ Tenés 6 alertas climáticas:")
        assert first.sent + second.sent == 6
        assert first.deferred + second.deferred == 0
        async with session_factory() as check:
            statuses = (
                await check.execute(
                    select(Notification.status).where(Notification.alert_id == alert.id)
                )
            ).scalars()
            assert list(statuses) == [NotificationStatus.SENT] * 6
            attempts = (
                await check.execute(
                    select(Notification.attempts).where(Notification.alert_id == alert.id)
                )
            ).scalars()
            # Cada notificación se envió exactamente una vez.
            assert list(attempts) == [1] * 6
    finally:
        async with engine.begin() as conn:
            await conn.execute(delete(User).where(User.phone_number == phone_number))
        await engine.dispose()


# --- Texto: todos los eventos y fechas límite -------------------------------
#
# `test_build_message_text_uses_the_spanish_label` solo comprueba que la etiqueta y la fecha
# aparezcan; estos tests fijan el string completo (docs/JOBS.md §Texto del mensaje).


@pytest.mark.parametrize(
    ("event_type", "label"),
    [
        (EventType.FROST, "helada"),
        (EventType.RAIN, "lluvia"),
        (EventType.HAIL, "granizo"),
        (EventType.STRONG_WIND, "viento fuerte"),
        (EventType.HEAT_WAVE, "ola de calor"),
    ],
)
@pytest.mark.parametrize(
    ("forecast_date", "day"),
    [
        (date(2026, 11, 18), "18/11/2026"),
        (date(2027, 1, 1), "01/01/2027"),
        (date(2026, 12, 31), "31/12/2026"),
    ],
)
def test_build_message_text_is_exact_for_every_event_and_boundary_date(
    event_type: EventType, label: str, forecast_date: date, day: str
) -> None:
    text = build_message_text(
        event_type=event_type,
        field_name="Lote Norte",
        probability=100,
        threshold=1,
        forecast_date=forecast_date,
    )

    assert text == (
        f'⚠️ Alerta de {label} en "Lote Norte": 100% de probabilidad para el {day} (tu umbral: 1%).'
    )


def test_build_message_text_keeps_quotes_accents_and_emoji_verbatim() -> None:
    field_name = 'Lote "Ñandú" 🌱 del Sur'

    text = build_message_text(
        event_type=EventType.HAIL,
        field_name=field_name,
        probability=0,
        threshold=100,
        forecast_date=date(2027, 1, 1),
    )

    assert text == (
        '⚠️ Alerta de granizo en "Lote "Ñandú" 🌱 del Sur": 0% de probabilidad '
        "para el 01/01/2027 (tu umbral: 100%)."
    )


# --- Enmascarado del teléfono ----------------------------------------------


@pytest.mark.parametrize(
    ("phone_number", "masked"),
    [
        ("12345678", "***5678"),  # 8 dígitos
        ("+54911223344556", "***4556"),  # 15 caracteres E.164
        ("+5491100000001", "***0001"),
    ],
)
def test_mask_phone_keeps_only_the_last_four_characters(phone_number: str, masked: str) -> None:
    assert mask_phone(phone_number) == masked


class _RepeatingLeakNotifier:
    """Notifier cuyo error repite el teléfono del destinatario varias veces."""

    async def send(self, message: NotificationMessage) -> None:
        raise NotifierError(
            f"a {message.phone_number} b {message.phone_number} c {message.phone_number}"
        )


async def test_every_occurrence_of_the_phone_is_masked_in_last_error(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    phone_number = "+54911223344556"
    owner = await make_user(phone_number=phone_number)
    alert = await make_alert(await make_field(owner))
    notification = await _make_notification(db_session, alert)

    await dispatch_pending(test_sessionmaker, _RepeatingLeakNotifier(), now=NOW, **DISPATCH_KWARGS)

    await db_session.refresh(notification)
    assert notification.last_error == ("NotifierError: a ***4556 b ***4556 c ***4556")
    assert phone_number not in (notification.last_error or "")


# --- PII: variantes del teléfono -------------------------------------------
#
# CR-2/SEC-1: no basta con enmascarar el string exacto. Un proveedor puede devolver el número
# sin `+`, troceado con separadores o URL-encoded (`%2B`).


@pytest.mark.parametrize(
    ("phone_number", "leak", "masked_leak"),
    [
        ("+5491122334455", "5491122334455", "***4455"),  # sin `+`
        ("+5491112345678", "+54 9 11 1234-5678", "***5678"),  # espacios y guiones
        ("+5491112345678", "%2B5491112345678", "***5678"),  # URL-encoded
        ("+5491112345678", "+54.9.11.1234.5678", "***5678"),  # puntos
        ("+5491112345678", "(54) 9 11 1234 5678", "(***5678"),  # paréntesis inicial
    ],
)
async def test_the_phone_is_masked_in_every_variant(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
    phone_number: str,
    leak: str,
    masked_leak: str,
) -> None:
    owner = await make_user(phone_number=phone_number)
    alert = await make_alert(await make_field(owner))
    notification = await _make_notification(db_session, alert)

    await dispatch_pending(
        test_sessionmaker,
        _FailingNotifier(NotifierError(f"provider rejected {leak}")),
        now=NOW,
        **DISPATCH_KWARGS,
    )

    await db_session.refresh(notification)
    assert notification.last_error == f"NotifierError: provider rejected {masked_leak}"
    digits = "".join(char for char in phone_number if char.isdigit())
    assert digits not in (notification.last_error or "")


async def test_repeated_variants_of_the_phone_are_all_masked(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    phone_number = "+5491112345678"
    leaked = f"{phone_number} / 5491112345678 / +54 9 11 1234-5678 / %2B5491112345678"
    owner = await make_user(phone_number=phone_number)
    alert = await make_alert(await make_field(owner))
    notification = await _make_notification(db_session, alert)

    await dispatch_pending(
        test_sessionmaker,
        _FailingNotifier(NotifierError(leaked)),
        now=NOW,
        **DISPATCH_KWARGS,
    )

    await db_session.refresh(notification)
    assert notification.last_error == "NotifierError: ***5678 / ***5678 / ***5678 / ***5678"
    digits = "".join(char for char in phone_number if char.isdigit())
    assert digits not in (notification.last_error or "")


# --- PII: variantes nacionales y wa_id (SEC-4) ------------------------------
#
# SEC-4: el enmascarado no puede exigir la secuencia E.164 completa. Meta devuelve el `wa_id`
# sin `+` ni el `9` móvil, y muchos proveedores usan el formato nacional (`011 15-...`).


@pytest.mark.parametrize(
    ("leak", "masked_leak"),
    [
        ("541122334455", "54***4455"),  # wa_id de Meta sin `+` ni el 9 móvil
        ("011 15-2233-4455", "***4455"),  # formato nacional: 0 + área + 15 + abonado
        ("+54/9/11/2233/4455", "***4455"),  # `/` como separador
        ("+54 9 11 2233 4455", "***4455"),  # control E.164 completo
    ],
)
def test_mask_phone_in_text_covers_the_national_and_wa_id_variants(
    leak: str, masked_leak: str
) -> None:
    phone_number = "+5491122334455"

    masked = mask_phone_in_text(f"provider rejected {leak}", phone_number)

    assert masked == f"provider rejected {masked_leak}"
    assert "".join(char for char in phone_number if char.isdigit()) not in masked


def test_mask_phone_in_text_leaves_a_different_number_intact() -> None:
    text = "provider rejected +54 9 11 5555 6666"

    assert mask_phone_in_text(text, "+5491122334455") == text


async def test_the_wa_id_variant_is_masked_in_last_error(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    owner = await make_user(phone_number="+5491122334455")
    alert = await make_alert(await make_field(owner))
    notification = await _make_notification(db_session, alert)

    await dispatch_pending(
        test_sessionmaker,
        _FailingNotifier(NotifierError("wa_id 541122334455 rejected")),
        now=NOW,
        **DISPATCH_KWARGS,
    )

    await db_session.refresh(notification)
    assert notification.last_error == "NotifierError: wa_id 54***4455 rejected"


# --- Tipos de excepción ----------------------------------------------------
#
# JOBS.md: "Excepción (cualquiera, incluido TimeoutError)". El dispatcher no debe depender de
# `NotifierError`.


@pytest.mark.parametrize(
    "error",
    [ValueError("bad payload"), RuntimeError("provider crashed")],
)
async def test_a_non_notifier_exception_is_retried_like_any_other(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    make_field: MakeField,
    make_alert: MakeAlert,
    error: Exception,
) -> None:
    alert = await make_alert(await make_field())
    notification = await _make_notification(db_session, alert)

    result = await dispatch_pending(
        test_sessionmaker,
        _FailingNotifier(error),
        now=NOW,
        clock=_clock_at(NOW),
        **DISPATCH_KWARGS,
    )

    assert result == DispatchResult(sent=0, retried=1, failed=0, users=1)
    await db_session.refresh(notification)
    assert notification.status == NotificationStatus.PENDING
    assert notification.attempts == 1
    assert notification.last_error == f"{type(error).__name__}: {error}"
    assert notification.next_attempt_at == NOW + timedelta(seconds=30)


async def test_an_exception_with_an_empty_message_still_records_the_type(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    alert = await make_alert(await make_field())
    notification = await _make_notification(db_session, alert)

    await dispatch_pending(
        test_sessionmaker, _FailingNotifier(NotifierError()), now=NOW, **DISPATCH_KWARGS
    )

    await db_session.refresh(notification)
    assert notification.last_error == "NotifierError: "
    assert notification.attempts == 1


# --- max_attempts = 1 ------------------------------------------------------


async def test_max_attempts_one_fails_on_the_first_error(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    alert = await make_alert(await make_field())
    notification = await _make_notification(db_session, alert)

    result = await dispatch_pending(
        test_sessionmaker,
        FakeNotifier(fail_times=1),
        now=NOW,
        **{**DISPATCH_KWARGS, "max_attempts": 1},
    )

    assert result == DispatchResult(sent=0, retried=0, failed=1, users=1)
    await db_session.refresh(notification)
    assert notification.status == NotificationStatus.FAILED
    assert notification.attempts == 1
    assert notification.last_error == "NotifierError: simulated failure"
    assert notification.next_attempt_at == NOW + timedelta(seconds=DISPATCH_KWARGS["lease_seconds"])
    assert notification.sent_at is None


# --- Orden de la cola ------------------------------------------------------


async def test_batch_size_one_takes_the_earliest_due_and_breaks_ties_by_user_id(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    """Orden `min(next_attempt_at), user_id` de los usuarios, y `next_attempt_at, id` de la cola."""

    async def _user_notification(due: datetime) -> Notification:
        owner = await make_user()
        alert = await make_alert(await make_field(owner))
        return await _make_notification(db_session, alert, next_attempt_at=due)

    late = await _user_notification(NOW + timedelta(seconds=10))
    early = await _user_notification(NOW)
    tied = await _user_notification(NOW)
    # `late` se creó primero (id menor) pero vence después: no debe reclamarse primero.
    assert late.id < early.id < tied.id
    notifier = FakeNotifier()
    kwargs = {**DISPATCH_KWARGS, "batch_size": 1}

    await dispatch_pending(test_sessionmaker, notifier, now=NOW, **kwargs)
    await dispatch_pending(test_sessionmaker, notifier, now=NOW, **kwargs)
    await dispatch_pending(test_sessionmaker, notifier, now=NOW + timedelta(seconds=10), **kwargs)

    assert [message.notification_id for message in notifier.sent] == [
        early.id,
        tied.id,
        late.id,
    ]
    for notification in (early, tied, late):
        await db_session.refresh(notification)
        assert notification.status == NotificationStatus.SENT


# --- Cola vacía ------------------------------------------------------------


async def test_an_empty_queue_reports_zeroes_and_sends_nothing(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    notifier = FakeNotifier()

    result = await dispatch_pending(test_sessionmaker, notifier, now=NOW, **DISPATCH_KWARGS)

    assert result == DispatchResult(sent=0, retried=0, failed=0)
    assert notifier.calls == 0


# --- Validación al enviar (S9/D12) -----------------------------------------


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("deleted", "alert_deleted"),
        ("inactive", "alert_inactive"),
        ("threshold", "below_current_threshold"),
    ],
    ids=["deleted-alert", "paused-alert", "threshold-raised"],
)
async def test_a_notification_that_no_longer_applies_is_cancelled_without_sending(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    mutation: str,
    reason: str,
) -> None:
    """Alerta borrada, pausada o con umbral vigente mayor: `cancelled`, nunca se envía."""
    alert = await make_alert(await make_field(), threshold=70)
    notification = await _make_notification(db_session, alert, probability=85)
    if mutation == "deleted":
        alert.deleted_at = NOW
    elif mutation == "inactive":
        alert.is_active = False
    else:
        alert.threshold = 90
    await db_session.flush()
    notifier = FakeNotifier()

    result = await dispatch_pending(test_sessionmaker, notifier, now=NOW, **DISPATCH_KWARGS)

    assert result == DispatchResult(sent=0, retried=0, failed=0, cancelled=1, users=1)
    assert notifier.calls == 0
    await db_session.refresh(notification)
    assert notification.status == NotificationStatus.CANCELLED
    assert notification.last_error == reason
    assert notification.attempts == 0
    assert notification.sent_at is None


async def test_a_mixed_batch_sends_only_the_valid_notifications(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    field = await make_field()
    valid_alert = await make_alert(field, event_type=EventType.FROST, threshold=70)
    deleted_alert = await make_alert(field, event_type=EventType.RAIN, threshold=70)
    inactive_alert = await make_alert(field, event_type=EventType.HAIL, threshold=70)
    raised_alert = await make_alert(field, event_type=EventType.HEAT_WAVE, threshold=70)
    valid = await _make_notification(db_session, valid_alert, probability=85)
    deleted = await _make_notification(db_session, deleted_alert, probability=85)
    inactive = await _make_notification(db_session, inactive_alert, probability=85)
    raised = await _make_notification(db_session, raised_alert, probability=60)
    deleted_alert.deleted_at = NOW
    inactive_alert.is_active = False
    raised_alert.threshold = 90
    await db_session.flush()
    notifier = FakeNotifier()

    result = await dispatch_pending(test_sessionmaker, notifier, now=NOW, **DISPATCH_KWARGS)

    assert result == DispatchResult(sent=1, retried=0, failed=0, cancelled=3, users=1)
    assert [message.notification_id for message in notifier.sent] == [valid.id]
    statuses = {}
    for notification in (valid, deleted, inactive, raised):
        await db_session.refresh(notification)
        statuses[notification.id] = (notification.status, notification.last_error)
    assert statuses == {
        valid.id: (NotificationStatus.SENT, None),
        deleted.id: (NotificationStatus.CANCELLED, "alert_deleted"),
        inactive.id: (NotificationStatus.CANCELLED, "alert_inactive"),
        raised.id: (NotificationStatus.CANCELLED, "below_current_threshold"),
    }


async def test_lowering_the_current_threshold_does_not_cancel_a_pending_notification(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    """D12: la re-validación es contra el umbral vigente; si bajó, la notificación se envía."""
    alert = await make_alert(await make_field(), threshold=70)
    notification = await _make_notification(db_session, alert, probability=60)
    alert.threshold = 50
    await db_session.flush()
    notifier = FakeNotifier()

    result = await dispatch_pending(test_sessionmaker, notifier, now=NOW, **DISPATCH_KWARGS)

    assert result == DispatchResult(sent=1, retried=0, failed=0, users=1)
    assert [message.notification_id for message in notifier.sent] == [notification.id]
    await db_session.refresh(notification)
    assert notification.status == NotificationStatus.SENT
    # El snapshot conserva el umbral con el que se creó la notificación.
    assert notification.threshold == 70


async def test_probability_equal_to_the_current_threshold_is_not_cancelled(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    """El límite es `probability < threshold` (no `<=`): igual al umbral vigente se envía."""
    alert = await make_alert(await make_field(), threshold=50)
    notification = await _make_notification(db_session, alert, probability=50)
    alert.threshold = 50
    await db_session.flush()
    notifier = FakeNotifier()

    result = await dispatch_pending(test_sessionmaker, notifier, now=NOW, **DISPATCH_KWARGS)

    assert result == DispatchResult(sent=1, retried=0, failed=0, users=1)
    assert [message.notification_id for message in notifier.sent] == [notification.id]
    await db_session.refresh(notification)
    assert notification.status == NotificationStatus.SENT


async def test_the_current_forecast_probability_equal_to_the_threshold_is_sent(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    """SEC-5: el borde `==` se compara contra el pronóstico vigente y no cancela.

    El snapshot de la notificación quedó en 85% (lo que disparó el evaluador), pero la ingesta
    bajó el pronóstico del día a 70%: con umbral 70% el aviso sigue correspondiendo.
    """
    alert = await make_alert(await make_field(), threshold=70)
    notification = await _make_notification(
        db_session, alert, probability=85, forecast_probability=70
    )
    notifier = FakeNotifier()

    result = await dispatch_pending(test_sessionmaker, notifier, now=NOW, **DISPATCH_KWARGS)

    assert result == DispatchResult(sent=1, retried=0, failed=0, users=1)
    assert [message.notification_id for message in notifier.sent] == [notification.id]
    await db_session.refresh(notification)
    assert notification.status == NotificationStatus.SENT
    assert notification.last_error is None


async def test_the_current_forecast_probability_below_the_threshold_cancels(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    """SEC-5: manda la probabilidad del pronóstico vigente, no el snapshot de la notificación.

    El evaluador avisó con 85%, pero la ingesta bajó el pronóstico del día a 60% y el umbral
    vigente es 70%: la notificación se cancela sin enviarse.
    """
    alert = await make_alert(await make_field(), threshold=70)
    notification = await _make_notification(
        db_session, alert, probability=85, forecast_probability=60
    )
    notifier = FakeNotifier()

    result = await dispatch_pending(test_sessionmaker, notifier, now=NOW, **DISPATCH_KWARGS)

    assert result == DispatchResult(sent=0, retried=0, failed=0, cancelled=1, users=1)
    assert notifier.calls == 0
    await db_session.refresh(notification)
    assert notification.status == NotificationStatus.CANCELLED
    assert notification.last_error == "below_current_threshold"
    assert notification.sent_at is None


async def test_a_second_episode_below_the_current_threshold_is_cancelled(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    """S12b: el episodio no cambia la re-validación del claim.

    Una notificación de episodio 2 se cancela igual que la 1 si el pronóstico vigente (o el
    umbral actual) ya no corresponde, y una de episodio 2 que sigue arriba se envía.
    """
    alert = await make_alert(await make_field(), threshold=70)
    cancelled = await _make_notification(
        db_session, alert, probability=95, episode=2, forecast_probability=60
    )
    notifier = FakeNotifier()

    result = await dispatch_pending(test_sessionmaker, notifier, now=NOW, **DISPATCH_KWARGS)

    assert result == DispatchResult(sent=0, retried=0, failed=0, cancelled=1, users=1)
    assert notifier.calls == 0
    await db_session.refresh(cancelled)
    assert cancelled.status == NotificationStatus.CANCELLED
    assert cancelled.episode == 2
    assert cancelled.last_error == "below_current_threshold"


async def test_a_second_episode_above_the_current_threshold_is_sent(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    """S12b: un aviso del episodio 2 con el pronóstico vigente sobre el umbral se envía."""
    alert = await make_alert(await make_field(), threshold=70)
    notification = await _make_notification(
        db_session,
        alert,
        probability=95,
        episode=2,
        forecast_probability=85,
    )
    notifier = FakeNotifier()

    result = await dispatch_pending(test_sessionmaker, notifier, now=NOW, **DISPATCH_KWARGS)

    assert result == DispatchResult(sent=1, retried=0, failed=0, users=1)
    assert [message.notification_id for message in notifier.sent] == [notification.id]
    await db_session.refresh(notification)
    assert notification.status == NotificationStatus.SENT
    assert notification.episode == 2


async def test_the_current_forecast_probability_above_the_threshold_is_sent(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    """SEC-5: si el pronóstico vigente sigue sobre el umbral, el aviso se envía."""
    alert = await make_alert(await make_field(), threshold=70)
    notification = await _make_notification(
        db_session, alert, probability=72, forecast_probability=85
    )
    notifier = FakeNotifier()

    result = await dispatch_pending(test_sessionmaker, notifier, now=NOW, **DISPATCH_KWARGS)

    assert result == DispatchResult(sent=1, retried=0, failed=0, users=1)
    assert [message.notification_id for message in notifier.sent] == [notification.id]
    await db_session.refresh(notification)
    assert notification.status == NotificationStatus.SENT


# --- Frescura del pronóstico en el claim (S11 CR-2) ------------------------


async def test_a_pending_notification_with_a_stale_forecast_is_cancelled_without_sending(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    """CR-2: si el pronóstico vigente venció, el claim cancela con `stale_forecast`."""
    alert = await make_alert(await make_field(), threshold=70)
    notification = await _make_notification(
        db_session,
        alert,
        probability=85,
        forecast_issued_at=NOW - timedelta(hours=24, seconds=1),
    )
    notifier = FakeNotifier()

    result = await dispatch_pending(test_sessionmaker, notifier, now=NOW, **DISPATCH_KWARGS)

    assert result == DispatchResult(sent=0, retried=0, failed=0, cancelled=1, users=1)
    assert notifier.calls == 0
    await db_session.refresh(notification)
    assert notification.status == NotificationStatus.CANCELLED
    assert notification.last_error == "stale_forecast"
    assert notification.sent_at is None
    assert notification.attempts == 0


async def test_a_pending_notification_without_a_forecast_is_cancelled_as_stale(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    """CR-2: si el pronóstico vigente ya no existe, tampoco se envía."""
    alert = await make_alert(await make_field(), threshold=70)
    notification = await _make_notification(
        db_session, alert, probability=85, forecast_issued_at=None
    )
    notifier = FakeNotifier()

    result = await dispatch_pending(test_sessionmaker, notifier, now=NOW, **DISPATCH_KWARGS)

    assert result == DispatchResult(sent=0, retried=0, failed=0, cancelled=1, users=1)
    assert notifier.calls == 0
    await db_session.refresh(notification)
    assert notification.last_error == "stale_forecast"


async def test_a_pending_notification_whose_forecast_was_deleted_is_cancelled_as_stale(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    """S12a: la ingesta borró la fila del pronóstico vigente → `stale_forecast`, no se envía."""
    alert = await make_alert(await make_field(), threshold=70)
    notification = await _make_notification(db_session, alert, probability=85)
    await db_session.execute(
        delete(WeatherForecast).where(WeatherForecast.field_id == alert.field_id)
    )
    notifier = FakeNotifier()

    result = await dispatch_pending(test_sessionmaker, notifier, now=NOW, **DISPATCH_KWARGS)

    assert result == DispatchResult(sent=0, retried=0, failed=0, cancelled=1, users=1)
    assert notifier.calls == 0
    await db_session.refresh(notification)
    assert notification.status == NotificationStatus.CANCELLED
    assert notification.last_error == "stale_forecast"
    assert notification.attempts == 0


async def test_a_pending_notification_at_the_freshness_boundary_is_sent(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    """CR-2: `issued_at == now - max_forecast_age_hours` sigue fresco (borde inclusivo)."""
    alert = await make_alert(await make_field(), threshold=70)
    notification = await _make_notification(
        db_session,
        alert,
        probability=85,
        forecast_issued_at=NOW - timedelta(hours=24),
    )
    notifier = FakeNotifier()

    result = await dispatch_pending(test_sessionmaker, notifier, now=NOW, **DISPATCH_KWARGS)

    assert result == DispatchResult(sent=1, retried=0, failed=0, users=1)
    assert [message.notification_id for message in notifier.sent] == [notification.id]
    await db_session.refresh(notification)
    assert notification.status == NotificationStatus.SENT


# --- Dueño del teléfono ----------------------------------------------------


async def test_each_notification_goes_to_its_own_owners_phone(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    ana = await make_user(phone_number="+5491100000001")
    bruno = await make_user(phone_number="+5491100000002")
    ana_alert = await make_alert(await make_field(ana), event_type=EventType.FROST)
    bruno_alert = await make_alert(
        await make_field(bruno), event_type=EventType.HEAT_WAVE, threshold=40
    )
    ana_notification = await _make_notification(db_session, ana_alert)
    bruno_notification = await _make_notification(
        db_session, bruno_alert, forecast_date=date(2026, 11, 19), probability=40
    )
    notifier = FakeNotifier()

    result = await dispatch_pending(test_sessionmaker, notifier, now=NOW, **DISPATCH_KWARGS)

    assert result == DispatchResult(sent=2, retried=0, failed=0, users=2)
    delivered = {message.notification_id: message.phone_number for message in notifier.sent}
    assert delivered == {
        ana_notification.id: "+5491100000001",
        bruno_notification.id: "+5491100000002",
    }


# --- Un fallo no corta el lote ---------------------------------------------


async def test_a_failure_does_not_stop_the_rest_of_the_batch(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    failing_alert = await make_alert(await make_field(await make_user()))
    failing = await _make_notification(db_session, failing_alert)
    ok_alert = await make_alert(await make_field(await make_user()))
    ok = await _make_notification(db_session, ok_alert)
    notifier = _SelectiveNotifier(failing_ids={failing.id})

    result = await dispatch_pending(test_sessionmaker, notifier, now=NOW, **DISPATCH_KWARGS)

    assert result == DispatchResult(sent=1, retried=1, failed=0, users=2)
    assert [message.notification_id for message in notifier.sent] == [ok.id]


# --- Idempotencia del envío ------------------------------------------------


async def test_a_sent_notification_is_never_sent_again(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    alert = await make_alert(await make_field())
    notification = await _make_notification(db_session, alert)

    first = FakeNotifier()
    await dispatch_pending(
        test_sessionmaker, first, now=NOW, clock=_clock_at(NOW), **DISPATCH_KWARGS
    )
    second = FakeNotifier()
    result = await dispatch_pending(
        test_sessionmaker, second, now=NOW + timedelta(minutes=1), **DISPATCH_KWARGS
    )

    assert result == DispatchResult(sent=0, retried=0, failed=0)
    assert first.calls == 1
    assert second.calls == 0
    await db_session.refresh(notification)
    assert notification.status == NotificationStatus.SENT
    assert notification.sent_at == NOW
    assert notification.attempts == 1


class _StateMutatingNotifier:
    """Simula un efecto secundario que escribe sobre la misma fila durante el envío."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.sent: list[NotificationMessage] = []

    async def send(self, message: NotificationMessage) -> None:
        self.sent.append(message)
        await self.session.execute(
            update(Notification)
            .where(Notification.id == message.notification_id)
            .values(status=NotificationStatus.PENDING, attempts=0)
        )


async def test_a_notifier_touching_the_row_does_not_undo_the_sent_state(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    alert = await make_alert(await make_field())
    notification = await _make_notification(db_session, alert)
    notifier = _StateMutatingNotifier(db_session)

    result = await dispatch_pending(
        test_sessionmaker, notifier, now=NOW, clock=_clock_at(NOW), **DISPATCH_KWARGS
    )

    assert result == DispatchResult(sent=1, retried=0, failed=0, users=1)
    assert len(notifier.sent) == 1
    await db_session.refresh(notification)
    assert notification.status == NotificationStatus.SENT
    assert notification.attempts == 1
    assert notification.sent_at == NOW

    resent = FakeNotifier()
    assert await dispatch_pending(
        test_sessionmaker, resent, now=NOW, **DISPATCH_KWARGS
    ) == DispatchResult(0, 0, 0)
    assert resent.calls == 0


# --- Logs sin PII ----------------------------------------------------------


async def test_no_dispatcher_or_logging_notifier_record_has_the_full_phone(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
    caplog: pytest.LogCaptureFixture,
) -> None:
    phone_number = "+5491188877766"
    owner = await make_user(phone_number=phone_number)
    alert = await make_alert(await make_field(owner))
    await _make_notification(db_session, alert)

    with caplog.at_level(logging.DEBUG):
        await dispatch_pending(test_sessionmaker, LoggingNotifier(), now=NOW, **DISPATCH_KWARGS)

    messages = [record.getMessage() for record in caplog.records]
    assert messages
    assert any("dispatch_done users=1 sent=1 retried=0 failed=0" in message for message in messages)
    assert any("phone=***7766" in message for message in messages)
    assert all(phone_number not in message for message in messages)


async def test_a_leaky_error_is_never_logged_by_the_dispatcher(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
    caplog: pytest.LogCaptureFixture,
) -> None:
    phone_number = "+54911223344556"
    owner = await make_user(phone_number=phone_number)
    alert = await make_alert(await make_field(owner))
    notification = await _make_notification(db_session, alert)
    notifier = _RepeatingLeakNotifier()

    with caplog.at_level(logging.DEBUG):
        await dispatch_pending(test_sessionmaker, notifier, now=NOW, **DISPATCH_KWARGS)

    assert caplog.records
    assert all(phone_number not in record.getMessage() for record in caplog.records)
    await db_session.refresh(notification)
    assert phone_number not in (notification.last_error or "")


# --- Lease: sin red dentro de una transacción (CR-1/SEC-2) ------------------


class _BlockingNotifier:
    """Avisa que empezó el envío y espera a que lo liberen (simula un proveedor lento)."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def send(self, message: NotificationMessage) -> None:
        self.started.set()
        await self.release.wait()


async def test_the_row_is_not_locked_while_the_network_call_is_in_flight() -> None:
    """El claim commitea antes de enviar: otra conexión puede lockear la fila mientras tanto."""
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    phone_number = f"+54911{uuid.uuid4().int % 10**8:08d}"
    notifier = _BlockingNotifier()
    try:
        async with session_factory() as setup:
            user = User(phone_number=phone_number, name=f"Lock probe {uuid.uuid4().hex}")
            setup.add(user)
            await setup.flush()
            field = Field(
                user_id=user.id,
                name=f"Lock probe field {uuid.uuid4().hex}",
                latitude=Decimal("-34.6"),
                longitude=Decimal("-58.4"),
            )
            setup.add(field)
            await setup.flush()
            alert = Alert(field_id=field.id, event_type=EventType.FROST, threshold=50)
            setup.add(alert)
            await setup.flush()
            setup.add(
                WeatherForecast(
                    field_id=field.id,
                    forecast_date=FORECAST_DATE,
                    event_type=EventType.FROST,
                    probability=80,
                    source="mock",
                    issued_at=NOW,
                    fetched_at=NOW,
                )
            )
            notification = Notification(
                alert_id=alert.id,
                forecast_date=FORECAST_DATE,
                event_type=EventType.FROST,
                probability=80,
                threshold=50,
                status=NotificationStatus.PENDING,
                attempts=0,
                next_attempt_at=NOW - timedelta(minutes=1),
            )
            setup.add(notification)
            await setup.commit()
            notification_id = notification.id

        dispatch = asyncio.create_task(
            dispatch_pending(session_factory, notifier, now=NOW, **DISPATCH_KWARGS)
        )
        try:
            await asyncio.wait_for(notifier.started.wait(), timeout=5)
            async with engine.connect() as probe:
                await probe.execute(text("SET LOCAL lock_timeout = '2s'"))
                locked = (
                    await probe.execute(
                        text("SELECT id FROM notifications WHERE id = :id FOR UPDATE NOWAIT"),
                        {"id": notification_id},
                    )
                ).first()
            assert locked is not None
        finally:
            notifier.release.set()

        assert await dispatch == DispatchResult(sent=1, retried=0, failed=0, users=1)
    finally:
        notifier.release.set()
        async with engine.begin() as conn:
            await conn.execute(delete(User).where(User.phone_number == phone_number))
        await engine.dispose()


class _AdvancingClock:
    """Reloj inyectable: devuelve cada instante una vez y luego repite el último."""

    def __init__(self, *moments: datetime) -> None:
        self._moments = list(moments)
        self.calls = 0

    def __call__(self) -> datetime:
        moment = self._moments[min(self.calls, len(self._moments) - 1)]
        self.calls += 1
        return moment


async def test_each_batch_leases_from_its_own_claim_time(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    """SEC-3: el lease no puede calcularse con un `now` viejo reusado entre lotes.

    Con un `now` congelado, el segundo lote nacería arrendado hasta una hora ya pasada y otra
    réplica lo reenviaría. Con el reloj avanzando, cada lease parte de la hora de su claim.
    """
    alert = await make_alert(await make_field())
    notification = await _make_notification(db_session, alert)
    lease_seconds = DISPATCH_KWARGS["lease_seconds"]
    second_claim = NOW + timedelta(seconds=2 * lease_seconds)
    clock = _AdvancingClock(NOW, second_claim)

    with pytest.raises(asyncio.CancelledError):
        await dispatch_pending(
            test_sessionmaker, _CancellingNotifier(), now=None, clock=clock, **DISPATCH_KWARGS
        )
    await db_session.refresh(notification)
    assert notification.next_attempt_at == NOW + timedelta(seconds=lease_seconds)

    # El lease del primero ya venció cuando corre el segundo claim.
    with pytest.raises(asyncio.CancelledError):
        await dispatch_pending(
            test_sessionmaker, _CancellingNotifier(), now=None, clock=clock, **DISPATCH_KWARGS
        )
    await db_session.refresh(notification)
    assert clock.calls == 2
    assert notification.next_attempt_at == second_claim + timedelta(seconds=lease_seconds)


async def test_a_stale_lease_owner_cannot_record_its_outcome(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    """SEC-3: el lease es token de propiedad; el dueño viejo no escribe ni cuenta su intento."""
    alert = await make_alert(await make_field())
    notification = await _make_notification(db_session, alert)
    lease_seconds = DISPATCH_KWARGS["lease_seconds"]

    worker_a = await _claim_batch(
        test_sessionmaker,
        now=NOW,
        batch_size=10,
        lease_seconds=lease_seconds,
        max_forecast_age_hours=DISPATCH_KWARGS["max_forecast_age_hours"],
        digest_max_items=DISPATCH_KWARGS["digest_max_items"],
    )
    assert worker_a.cancelled == 0
    assert [item.id for item in worker_a.claimed] == [notification.id]
    assert worker_a.claimed[0].lease_until == NOW + timedelta(seconds=lease_seconds)

    # El lease de A vence y B reclama la misma fila con una hora posterior.
    later = NOW + timedelta(seconds=lease_seconds + 1)
    worker_b = await _claim_batch(
        test_sessionmaker,
        now=later,
        batch_size=10,
        lease_seconds=lease_seconds,
        max_forecast_age_hours=DISPATCH_KWARGS["max_forecast_age_hours"],
        digest_max_items=DISPATCH_KWARGS["digest_max_items"],
    )
    assert worker_b.cancelled == 0
    assert [item.id for item in worker_b.claimed] == [notification.id]
    assert worker_b.claimed[0].lease_until == later + timedelta(seconds=lease_seconds)
    assert worker_b.claimed[0].lease_until != worker_a.claimed[0].lease_until

    recorded_by_b = await _record_outcomes(
        test_sessionmaker,
        [_SendOutcome(worker_b.claimed[0], None)],
        clock=_clock_at(later),
        max_attempts=DISPATCH_KWARGS["max_attempts"],
        backoff_base_seconds=DISPATCH_KWARGS["backoff_base_seconds"],
    )
    assert recorded_by_b == DispatchResult(sent=1, retried=0, failed=0)

    recorded_by_a = await _record_outcomes(
        test_sessionmaker,
        [_SendOutcome(worker_a.claimed[0], None)],
        clock=_clock_at(later + timedelta(seconds=1)),
        max_attempts=DISPATCH_KWARGS["max_attempts"],
        backoff_base_seconds=DISPATCH_KWARGS["backoff_base_seconds"],
    )
    assert recorded_by_a == DispatchResult(sent=0, retried=0, failed=0)

    await db_session.refresh(notification)
    assert notification.status == NotificationStatus.SENT
    assert notification.attempts == 1
    assert notification.sent_at == later


class _ConcurrencyProbeNotifier:
    """Mide cuántos envíos hay en vuelo a la vez."""

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.calls = 0

    async def send(self, message: NotificationMessage) -> None:
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.02)
        finally:
            self.active -= 1


async def test_concurrency_bounds_the_number_of_simultaneous_sends(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    for _ in range(5):
        alert = await make_alert(await make_field(await make_user()))
        await _make_notification(db_session, alert, forecast_date=FORECAST_DATE + timedelta(days=1))
    notifier = _ConcurrencyProbeNotifier()

    result = await dispatch_pending(
        test_sessionmaker,
        notifier,
        now=NOW,
        **{**DISPATCH_KWARGS, "concurrency": 2, "timeout_seconds": 5.0},
    )

    assert result == DispatchResult(sent=5, retried=0, failed=0, users=5)
    assert notifier.calls == 5
    assert notifier.max_active == 2


class _AlertDeletingNotifier:
    """Simula un `DELETE /alerts/{id}` (en cascada) mientras el envío está en vuelo."""

    def __init__(self, session: AsyncSession, alert_id: int) -> None:
        self.session = session
        self.alert_id = alert_id

    async def send(self, message: NotificationMessage) -> None:
        await self.session.execute(delete(Alert).where(Alert.id == self.alert_id))


async def test_a_notification_deleted_between_the_claim_and_the_update_is_ignored(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    alert = await make_alert(await make_field())
    notification = await _make_notification(db_session, alert)

    result = await dispatch_pending(
        test_sessionmaker,
        _AlertDeletingNotifier(db_session, alert.id),
        now=NOW,
        **DISPATCH_KWARGS,
    )

    assert result == DispatchResult(sent=0, retried=0, failed=0, users=1)
    remaining = await db_session.scalar(
        select(func.count()).select_from(Notification).where(Notification.id == notification.id)
    )
    assert remaining == 0


async def test_soft_deleting_the_alert_while_the_send_is_in_flight_keeps_the_real_outcome() -> None:
    """SEC-2/D11/D12: DELETE con la notificación ya reclamada y el envío en vuelo.

    El `DELETE` solo cancela las pendientes no arrendadas (`next_attempt_at <= now()`). La fila
    en vuelo queda `pending` con su lease vigente, así que el dispatcher registra el resultado
    real: `sent`, `attempts=1`, `sent_at` seteado. El historial nunca dice `cancelled` para un
    mensaje que salió.

    El claim usa una hora fija futura (`LEASE_SAFE_NOW`) para que el lease quede por delante
    del reloj real de la base que usa el `DELETE` (`now()`), sin depender del reloj del test.
    """
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    phone_number = f"+54911{uuid.uuid4().int % 10**8:08d}"
    notifier = _BlockingNotifier()
    try:
        async with session_factory() as setup:
            user = User(phone_number=phone_number, name=f"QA inflight delete {uuid.uuid4().hex}")
            setup.add(user)
            await setup.flush()
            field = Field(
                user_id=user.id,
                name=f"QA inflight field {uuid.uuid4().hex}",
                latitude=Decimal("-34.6"),
                longitude=Decimal("-58.4"),
            )
            setup.add(field)
            await setup.flush()
            alert = Alert(field_id=field.id, event_type=EventType.FROST, threshold=50)
            setup.add(alert)
            await setup.flush()
            setup.add(
                WeatherForecast(
                    field_id=field.id,
                    forecast_date=LEASE_SAFE_FORECAST_DATE,
                    event_type=EventType.FROST,
                    probability=80,
                    source="mock",
                    issued_at=LEASE_SAFE_NOW,
                    fetched_at=LEASE_SAFE_NOW,
                )
            )
            notification = Notification(
                alert_id=alert.id,
                forecast_date=LEASE_SAFE_FORECAST_DATE,
                event_type=EventType.FROST,
                probability=80,
                threshold=50,
                status=NotificationStatus.PENDING,
                attempts=0,
                next_attempt_at=NOW - timedelta(minutes=1),
            )
            setup.add(notification)
            await setup.commit()
            user_id = user.id
            alert_id = alert.id
            notification_id = notification.id

        dispatch = asyncio.create_task(
            dispatch_pending(
                session_factory,
                notifier,
                now=LEASE_SAFE_NOW,
                clock=_clock_at(LEASE_SAFE_NOW),
                **{**DISPATCH_KWARGS, "timeout_seconds": 10.0},
            )
        )
        try:
            await asyncio.wait_for(notifier.started.wait(), timeout=5)
            # El DELETE de la API, en su propia conexión y commiteado, con el envío en vuelo.
            async with session_factory() as api_session:
                await delete_alert(api_session, user_id, alert_id)
        finally:
            notifier.release.set()

        result = await dispatch
        assert result == DispatchResult(sent=1, retried=0, failed=0, users=1)

        async with session_factory() as check:
            stored_alert = (
                await check.execute(select(Alert).where(Alert.id == alert_id))
            ).scalar_one()
            stored_notification = (
                await check.execute(select(Notification).where(Notification.id == notification_id))
            ).scalar_one()
        assert stored_alert.deleted_at is not None
        assert stored_notification.status == NotificationStatus.SENT
        assert stored_notification.last_error is None
        assert stored_notification.attempts == 1
        assert stored_notification.sent_at == LEASE_SAFE_NOW

        # El próximo ciclo no encuentra nada pendiente: no hay reenvío.
        assert await dispatch_pending(
            session_factory,
            FakeNotifier(),
            now=LEASE_SAFE_NOW + timedelta(minutes=2),
            **DISPATCH_KWARGS,
        ) == DispatchResult(sent=0, retried=0, failed=0)
    finally:
        notifier.release.set()
        async with engine.begin() as conn:
            await conn.execute(delete(User).where(User.phone_number == phone_number))
        await engine.dispose()


async def test_a_leased_notification_of_a_deleted_alert_is_cancelled_when_the_lease_expires() -> (
    None
):
    """SEC-2: el worker muere con la fila arrendada; al vencer el lease, el claim la cancela.

    El DELETE no toca la fila (lease vigente). Cuando el lease vence, el próximo claim la
    selecciona y la validación D12 la pasa a `cancelled` con `alert_deleted`, sin enviarla.
    """
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    phone_number = f"+54911{uuid.uuid4().int % 10**8:08d}"
    notifier = _BlockingNotifier()
    try:
        async with session_factory() as setup:
            user = User(phone_number=phone_number, name=f"QA dead worker {uuid.uuid4().hex}")
            setup.add(user)
            await setup.flush()
            field = Field(
                user_id=user.id,
                name=f"QA dead worker field {uuid.uuid4().hex}",
                latitude=Decimal("-34.6"),
                longitude=Decimal("-58.4"),
            )
            setup.add(field)
            await setup.flush()
            alert = Alert(field_id=field.id, event_type=EventType.FROST, threshold=50)
            setup.add(alert)
            await setup.flush()
            setup.add(
                WeatherForecast(
                    field_id=field.id,
                    forecast_date=LEASE_SAFE_FORECAST_DATE,
                    event_type=EventType.FROST,
                    probability=80,
                    source="mock",
                    issued_at=LEASE_SAFE_NOW,
                    fetched_at=LEASE_SAFE_NOW,
                )
            )
            notification = Notification(
                alert_id=alert.id,
                forecast_date=LEASE_SAFE_FORECAST_DATE,
                event_type=EventType.FROST,
                probability=80,
                threshold=50,
                status=NotificationStatus.PENDING,
                attempts=0,
                next_attempt_at=NOW - timedelta(minutes=1),
            )
            setup.add(notification)
            await setup.commit()
            user_id = user.id
            alert_id = alert.id
            notification_id = notification.id

        dispatch = asyncio.create_task(
            dispatch_pending(
                session_factory,
                notifier,
                now=LEASE_SAFE_NOW,
                clock=_clock_at(LEASE_SAFE_NOW),
                **{**DISPATCH_KWARGS, "timeout_seconds": 10.0},
            )
        )
        await asyncio.wait_for(notifier.started.wait(), timeout=5)
        # El worker muere con el envío en vuelo: la fila queda `pending` y arrendada.
        dispatch.cancel()
        with pytest.raises(asyncio.CancelledError):
            await dispatch

        async with session_factory() as api_session:
            await delete_alert(api_session, user_id, alert_id)

        async with session_factory() as check:
            leased = (
                await check.execute(select(Notification).where(Notification.id == notification_id))
            ).scalar_one()
        assert leased.status == NotificationStatus.PENDING
        assert leased.next_attempt_at == LEASE_SAFE_NOW + timedelta(
            seconds=DISPATCH_KWARGS["lease_seconds"]
        )

        # Al vencer el lease, el claim re-valida contra la alerta borrada y la cancela.
        result = await dispatch_pending(
            session_factory,
            FakeNotifier(),
            now=LEASE_SAFE_NOW + timedelta(seconds=DISPATCH_KWARGS["lease_seconds"] + 1),
            **DISPATCH_KWARGS,
        )
        assert result == DispatchResult(sent=0, retried=0, failed=0, cancelled=1, users=1)

        async with session_factory() as check:
            stored = (
                await check.execute(select(Notification).where(Notification.id == notification_id))
            ).scalar_one()
        assert stored.status == NotificationStatus.CANCELLED
        assert stored.last_error == "alert_deleted"
        assert stored.attempts == 0
        assert stored.sent_at is None
    finally:
        notifier.release.set()
        async with engine.begin() as conn:
            await conn.execute(delete(User).where(User.phone_number == phone_number))
        await engine.dispose()


# --- Settings: el lease cubre el peor caso del lote ------------------------


def test_settings_reject_a_lease_that_does_not_cover_the_worst_case_batch() -> None:
    """`lease > ceil(batch / concurrency) * timeout`, si no otro worker reclamaría en vuelo."""
    worst_case = {
        "dispatch_batch_size": 100,
        "dispatch_concurrency": 10,
        "notifier_timeout_seconds": 10.0,
    }

    with pytest.raises(ValidationError, match="dispatch_lease_seconds"):
        Settings(dispatch_lease_seconds=100, **worst_case)

    settings = Settings(dispatch_lease_seconds=101, **worst_case)
    assert settings.dispatch_lease_seconds == 101


# --- Texto del digest (S13) -------------------------------------------------
#
# docs/JOBS.md §Texto del digest: exacto, ordenado por
# `(forecast_date, field_name, event_type.value, threshold, id)`, sin `\n` final.


def _claimed(
    *,
    id: int = 1,
    user_id: int = 1,
    forecast_date: date = FORECAST_DATE,
    event_type: EventType = EventType.FROST,
    probability: int = 85,
    threshold: int = 70,
    attempts: int = 0,
    phone_number: str = "+5491100000001",
    field_name: str = "Lote Norte",
) -> ClaimedNotification:
    """Un `ClaimedNotification` plano para probar el texto sin pasar por la DB."""
    return ClaimedNotification(
        id=id,
        user_id=user_id,
        forecast_date=forecast_date,
        event_type=event_type,
        probability=probability,
        threshold=threshold,
        attempts=attempts,
        phone_number=phone_number,
        field_name=field_name,
        lease_until=NOW + timedelta(seconds=60),
    )


def test_build_digest_text_matches_jobs_md_exactly() -> None:
    text = build_digest_text(
        [
            _claimed(
                id=1,
                event_type=EventType.FROST,
                field_name="Lote Norte",
                probability=85,
                threshold=70,
                forecast_date=date(2026, 11, 18),
            ),
            _claimed(
                id=2,
                event_type=EventType.HAIL,
                field_name="Lote Sur",
                probability=60,
                threshold=50,
                forecast_date=date(2026, 11, 19),
            ),
        ]
    )

    assert text == (
        "⚠️ Tenés 2 alertas climáticas:\n"
        '• Helada en "Lote Norte": 85% de probabilidad para el 18/11/2026 (tu umbral: 70%).\n'
        '• Granizo en "Lote Sur": 60% de probabilidad para el 19/11/2026 (tu umbral: 50%).'
    )
    assert not text.endswith("\n")


def test_build_digest_text_capitalizes_every_spanish_label() -> None:
    text = build_digest_text(
        [
            _claimed(id=1, event_type=EventType.HEAT_WAVE),
            _claimed(id=2, event_type=EventType.STRONG_WIND),
        ]
    )

    labels = [line.split(" en ")[0].lstrip("• ") for line in text.splitlines()[1:]]
    assert labels == ["Ola de calor", "Viento fuerte"]


def test_build_digest_text_sorts_by_date_field_event_threshold_and_id() -> None:
    items = [
        _claimed(id=4, forecast_date=date(2026, 11, 19), field_name="A", threshold=10),
        _claimed(id=3, forecast_date=date(2026, 11, 18), field_name="B", threshold=10),
        _claimed(id=2, forecast_date=date(2026, 11, 18), field_name="A", threshold=90),
        _claimed(id=1, forecast_date=date(2026, 11, 18), field_name="A", threshold=50),
    ]

    lines = build_digest_text(items).splitlines()

    assert [line.split("(tu umbral: ")[1].rstrip("%).") for line in lines[1:]] == [
        "50",
        "90",
        "10",
        "10",
    ]
    assert '"A"' in lines[1] and "18/11/2026" in lines[1]
    assert '"A"' in lines[2] and "18/11/2026" in lines[2]
    assert '"B"' in lines[3] and "18/11/2026" in lines[3]
    assert '"A"' in lines[4] and "19/11/2026" in lines[4]


def test_build_digest_text_requires_at_least_two_notifications() -> None:
    with pytest.raises(ValueError, match="at least 2"):
        build_digest_text([_claimed()])
    with pytest.raises(ValueError, match="at least 2"):
        build_digest_text([])


# --- Envío agrupado por usuario (S13) ---------------------------------------


async def test_two_due_notifications_of_the_same_user_go_out_in_one_digest(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    owner = await make_user(phone_number="+5491100000003")
    frost_alert = await make_alert(
        await make_field(owner, name="Lote Norte"),
        event_type=EventType.FROST,
        threshold=70,
    )
    hail_alert = await make_alert(
        await make_field(owner, name="Lote Sur"),
        event_type=EventType.HAIL,
        threshold=50,
    )
    first = await _make_notification(
        db_session, frost_alert, forecast_date=date(2026, 11, 18), probability=85
    )
    second = await _make_notification(
        db_session, hail_alert, forecast_date=date(2026, 11, 19), probability=60
    )
    notifier = FakeNotifier()

    result = await dispatch_pending(test_sessionmaker, notifier, now=NOW, **DISPATCH_KWARGS)

    assert result == DispatchResult(sent=2, retried=0, failed=0, users=1)
    assert len(notifier.sent) == 1
    message = notifier.sent[0]
    # `notification_id` = el menor id del grupo.
    assert message.notification_id == min(first.id, second.id)
    assert message.phone_number == "+5491100000003"
    assert message.text == (
        "⚠️ Tenés 2 alertas climáticas:\n"
        '• Helada en "Lote Norte": 85% de probabilidad para el 18/11/2026 (tu umbral: 70%).\n'
        '• Granizo en "Lote Sur": 60% de probabilidad para el 19/11/2026 (tu umbral: 50%).'
    )
    for notification in (first, second):
        await db_session.refresh(notification)
        assert notification.status == NotificationStatus.SENT
        assert notification.attempts == 1


async def test_each_user_gets_its_own_message_in_the_same_batch(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    ana = await make_user(phone_number="+5491100000011")
    bruno = await make_user(phone_number="+5491100000012")
    ana_alert = await make_alert(await make_field(ana))
    bruno_alert = await make_alert(await make_field(bruno), event_type=EventType.RAIN, threshold=40)
    await _make_notification(db_session, ana_alert)
    await _make_notification(db_session, bruno_alert, probability=60)
    notifier = FakeNotifier()

    result = await dispatch_pending(test_sessionmaker, notifier, now=NOW, **DISPATCH_KWARGS)

    assert result == DispatchResult(sent=2, retried=0, failed=0, users=2)
    by_phone = {message.phone_number: message for message in notifier.sent}
    assert set(by_phone) == {"+5491100000011", "+5491100000012"}
    assert "Tenés" not in by_phone["+5491100000011"].text


async def test_more_than_digest_max_items_leaves_the_excess_pending_and_due(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    owner = await make_user()
    alert = await make_alert(await make_field(owner))
    notifications = [
        await _make_notification(
            db_session, alert, forecast_date=date(2026, 11, 18) + timedelta(days=offset)
        )
        for offset in range(3)
    ]
    notifier = FakeNotifier()
    kwargs = {**DISPATCH_KWARGS, "digest_max_items": 2}

    first = await dispatch_pending(test_sessionmaker, notifier, now=NOW, **kwargs)

    assert first == DispatchResult(sent=2, retried=0, failed=0, users=1)
    assert len(notifier.sent) == 1
    assert notifier.sent[0].text.startswith("⚠️ Tenés 2 alertas climáticas:")
    await db_session.refresh(notifications[2])
    assert notifications[2].status == NotificationStatus.PENDING
    # El excedente queda vencido: sale en el lote siguiente.
    assert notifications[2].next_attempt_at == NOW

    second = await dispatch_pending(test_sessionmaker, notifier, now=NOW, **kwargs)

    assert second == DispatchResult(sent=1, retried=0, failed=0, users=1)
    assert notifier.sent[1].notification_id == notifications[2].id
    assert "Tenés" not in notifier.sent[1].text
    await db_session.refresh(notifications[2])
    assert notifications[2].status == NotificationStatus.SENT


async def test_a_failed_digest_applies_its_own_attempts_to_each_notification(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    owner = await make_user()
    alert = await make_alert(await make_field(owner))
    retried = await _make_notification(db_session, alert, forecast_date=date(2026, 11, 18))
    exhausted = await _make_notification(
        db_session, alert, forecast_date=date(2026, 11, 19), attempts=2
    )

    result = await dispatch_pending(
        test_sessionmaker,
        _FailingNotifier(NotifierError("boom")),
        now=NOW,
        **{**DISPATCH_KWARGS, "clock": _clock_at(NOW)},
    )

    assert result == DispatchResult(sent=0, retried=1, failed=1, users=1)
    await db_session.refresh(retried)
    assert retried.status == NotificationStatus.PENDING
    assert retried.attempts == 1
    assert retried.next_attempt_at == NOW + timedelta(seconds=30)
    await db_session.refresh(exhausted)
    assert exhausted.status == NotificationStatus.FAILED
    assert exhausted.attempts == 3
    # El error del mensaje se aplica a todas, con el teléfono enmascarado.
    assert retried.last_error == "NotifierError: boom"
    assert exhausted.last_error == "NotifierError: boom"


# --- Horario de silencio (S13) ---------------------------------------------

#: 2026-11-16 17:30 UTC = 14:30 en Buenos Aires (UTC-3).
QUIET_NOW = datetime(2026, 11, 16, 17, 30, tzinfo=UTC)
#: Fin de una ventana 13:00-15:00 local.
QUIET_END = datetime(2026, 11, 16, 18, 0, tzinfo=UTC)


async def test_a_user_in_quiet_hours_gets_everything_deferred_and_nothing_sent(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    owner = await make_user()
    owner.quiet_hours_start = time(13, 0)
    owner.quiet_hours_end = time(15, 0)
    await db_session.flush()
    alert = await make_alert(await make_field(owner))
    notifications = [
        await _make_notification(
            db_session, alert, forecast_date=date(2026, 11, 18) + timedelta(days=offset)
        )
        for offset in range(2)
    ]
    notifier = FakeNotifier()

    result = await dispatch_pending(
        test_sessionmaker,
        notifier,
        now=QUIET_NOW,
        clock=_clock_at(QUIET_NOW),
        **DISPATCH_KWARGS,
    )

    assert result == DispatchResult(sent=0, retried=0, failed=0, cancelled=0, deferred=2, users=1)
    assert notifier.calls == 0
    for notification in notifications:
        await db_session.refresh(notification)
        assert notification.status == NotificationStatus.PENDING
        assert notification.next_attempt_at == QUIET_END
        # El silencio no es un intento: ni `attempts` ni `last_error` cambian.
        assert notification.attempts == 0
        assert notification.last_error is None
        assert notification.sent_at is None

    # Al terminar el silencio salen todas juntas, en un solo mensaje.
    sent_now = QUIET_END
    sent = await dispatch_pending(
        test_sessionmaker, notifier, now=sent_now, clock=_clock_at(sent_now), **DISPATCH_KWARGS
    )

    assert sent == DispatchResult(sent=2, retried=0, failed=0, users=1)
    assert len(notifier.sent) == 1
    assert notifier.sent[0].text.startswith("⚠️ Tenés 2 alertas climáticas:")
    for notification in notifications:
        await db_session.refresh(notification)
        assert notification.status == NotificationStatus.SENT
        assert notification.attempts == 1


async def test_a_window_crossing_midnight_defers_until_tomorrow(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    owner = await make_user()
    owner.quiet_hours_start = time(22, 0)
    owner.quiet_hours_end = time(7, 0)
    await db_session.flush()
    alert = await make_alert(await make_field(owner))
    notification = await _make_notification(db_session, alert)
    notifier = FakeNotifier()

    # 2026-11-17 02:30 UTC = 23:30 local del 16/11.
    result = await dispatch_pending(
        test_sessionmaker,
        notifier,
        now=QUIET_NOW + timedelta(hours=9),
        clock=_clock_at(QUIET_NOW + timedelta(hours=9)),
        **DISPATCH_KWARGS,
    )

    assert result == DispatchResult(sent=0, retried=0, failed=0, cancelled=0, deferred=1, users=1)
    assert notifier.calls == 0
    await db_session.refresh(notification)
    # 07:00 local del 17/11 = 10:00 UTC.
    assert notification.next_attempt_at == datetime(2026, 11, 17, 10, 0, tzinfo=UTC)


async def test_an_invalid_user_timezone_is_logged_and_the_message_is_sent(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
    caplog: pytest.LogCaptureFixture,
) -> None:
    owner = await make_user()
    owner.timezone = "Mars/Olympus"
    owner.quiet_hours_start = time(13, 0)
    owner.quiet_hours_end = time(15, 0)
    await db_session.flush()
    alert = await make_alert(await make_field(owner))
    await _make_notification(db_session, alert)
    notifier = FakeNotifier()

    with caplog.at_level(logging.WARNING, logger="app.jobs.dispatcher"):
        result = await dispatch_pending(
            test_sessionmaker, notifier, now=QUIET_NOW, **DISPATCH_KWARGS
        )

    assert result == DispatchResult(sent=1, retried=0, failed=0, users=1)
    assert notifier.calls == 1
    assert any(
        f"invalid_user_timezone user_id={owner.id}" in record.getMessage()
        for record in caplog.records
    )


async def test_a_notification_cancelled_during_the_silence_is_not_deferred(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    owner = await make_user()
    owner.quiet_hours_start = time(13, 0)
    owner.quiet_hours_end = time(15, 0)
    await db_session.flush()
    alert = await make_alert(await make_field(owner))
    notification = await _make_notification(db_session, alert)
    alert.deleted_at = QUIET_NOW
    await db_session.flush()
    notifier = FakeNotifier()

    result = await dispatch_pending(test_sessionmaker, notifier, now=QUIET_NOW, **DISPATCH_KWARGS)

    assert result == DispatchResult(sent=0, retried=0, failed=0, cancelled=1, deferred=0, users=1)
    assert notifier.calls == 0
    await db_session.refresh(notification)
    assert notification.status == NotificationStatus.CANCELLED
    assert notification.last_error == "alert_deleted"


# --- Cancelación por día pronosticado pasado (S13) -------------------------


async def test_a_notification_for_a_past_day_in_the_field_timezone_is_cancelled(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    """NOW (16/11 12:00Z) es 16/11 09:00 en Buenos Aires: el 15/11 ya pasó."""
    alert = await make_alert(await make_field(await make_user()))
    notification = await _make_notification(db_session, alert, forecast_date=date(2026, 11, 15))
    notifier = FakeNotifier()

    result = await dispatch_pending(test_sessionmaker, notifier, now=NOW, **DISPATCH_KWARGS)

    assert result == DispatchResult(sent=0, retried=0, failed=0, cancelled=1, users=1)
    assert notifier.calls == 0
    await db_session.refresh(notification)
    assert notification.status == NotificationStatus.CANCELLED
    assert notification.last_error == "forecast_date_passed"
    assert notification.attempts == 0


async def test_the_past_day_check_uses_the_field_timezone_not_the_utc_date(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    """A las 12:00Z del 16/11 en Kiritimati (+14) ya es el 17: el 16 está pasado.

    El mismo día en un campo de Buenos Aires sigue siendo hoy y se envía.
    """
    east = await make_field(await make_user(), name="Lote +14")
    east.timezone = "Pacific/Kiritimati"
    west = await make_field(await make_user(), name="Lote -3")
    await db_session.flush()
    east_notification = await _make_notification(
        db_session, await make_alert(east), forecast_date=date(2026, 11, 16)
    )
    west_notification = await _make_notification(
        db_session, await make_alert(west), forecast_date=date(2026, 11, 16)
    )
    notifier = FakeNotifier()

    result = await dispatch_pending(test_sessionmaker, notifier, now=NOW, **DISPATCH_KWARGS)

    assert result == DispatchResult(sent=1, retried=0, failed=0, cancelled=1, users=2)
    assert [message.notification_id for message in notifier.sent] == [west_notification.id]
    await db_session.refresh(east_notification)
    assert east_notification.status == NotificationStatus.CANCELLED
    assert east_notification.last_error == "forecast_date_passed"
    await db_session.refresh(west_notification)
    assert west_notification.status == NotificationStatus.SENT


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("deleted", "alert_deleted"),
        ("inactive", "alert_inactive"),
    ],
)
async def test_the_cancellation_order_puts_the_alert_state_before_the_past_day(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
    mutation: str,
    reason: str,
) -> None:
    """Orden: alerta borrada > inactiva > día pasado > umbral > frescura (JOBS.md §S13)."""
    alert = await make_alert(await make_field(), threshold=70)
    notification = await _make_notification(db_session, alert, forecast_date=date(2026, 11, 14))
    if mutation == "deleted":
        alert.deleted_at = NOW
    else:
        alert.is_active = False
    await db_session.flush()

    result = await dispatch_pending(test_sessionmaker, FakeNotifier(), now=NOW, **DISPATCH_KWARGS)

    assert result.cancelled == 1
    await db_session.refresh(notification)
    assert notification.last_error == reason


async def test_the_past_day_beats_the_current_threshold(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    make_field: MakeField,
    make_alert: MakeAlert,
) -> None:
    """El día pasado se revisa antes que el umbral vigente (JOBS.md §S13)."""
    alert = await make_alert(await make_field(), threshold=70)
    notification = await _make_notification(
        db_session, alert, probability=60, forecast_date=date(2026, 11, 14)
    )
    alert.threshold = 90
    await db_session.flush()

    await dispatch_pending(test_sessionmaker, FakeNotifier(), now=NOW, **DISPATCH_KWARGS)

    await db_session.refresh(notification)
    assert notification.last_error == "forecast_date_passed"
