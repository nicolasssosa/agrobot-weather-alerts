"""Despacho de notificaciones pendientes (outbox, patrón lease).

Spec: docs/JOBS.md §Dispatcher. Regla de oro: nunca se espera a la red con una transacción
abierta. El lote se reclama y se arrienda (`next_attempt_at = now + lease`) en una transacción
corta; los envíos corren fuera de transacción, en paralelo acotado; y los resultados se persisten
en otra transacción corta. Las filas reclamadas quedan `pending` pero invisibles para los demás
workers hasta que vence el lease, así que un proceso que muere entre el envío y el commit no deja
locks: la notificación se reenvía sola al vencer el lease (semántica at-least-once, D10).
"""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.pii import mask_phone_in_text
from app.models import (
    Alert,
    EventType,
    Field,
    Notification,
    NotificationStatus,
    User,
    WeatherForecast,
)
from app.notifiers.base import NotificationMessage, Notifier

logger = logging.getLogger(__name__)

#: Tope de `notifications.last_error` (CHECK `last_error_length`, DATA_MODEL.md).
LAST_ERROR_MAX_LENGTH = 1000


def _utc_now() -> datetime:
    """Reloj real por defecto; inyectable en tests (nunca se usa en ellos el reloj real)."""
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """Conteo del lote: enviadas, reintentadas, agotadas (`failed`) y canceladas."""

    sent: int
    retried: int
    failed: int
    #: Notificaciones que ya no correspondía enviar (alerta borrada/inactiva/umbral o
    #: pronóstico vencido).
    cancelled: int = 0


@dataclass(frozen=True, slots=True)
class ClaimedNotification:
    """Snapshot plano de una notificación reclamada, leído antes de cerrar la sesión."""

    id: int
    forecast_date: date
    event_type: EventType
    probability: int
    threshold: int
    attempts: int
    phone_number: str
    field_name: str
    #: Valor exacto de `next_attempt_at` que dejó el claim: token de propiedad del lote.
    lease_until: datetime


@dataclass(frozen=True, slots=True)
class _SendOutcome:
    """Resultado de un envío: el claim y el error enmascarado (`None` si salió bien)."""

    claimed: ClaimedNotification
    error: str | None


def build_message_text(
    *,
    event_type: EventType,
    field_name: str,
    probability: int,
    threshold: int,
    forecast_date: date,
) -> str:
    """Arma el texto exacto del aviso (docs/JOBS.md §Texto del mensaje)."""
    day = forecast_date.strftime("%d/%m/%Y")
    return (
        f'⚠️ Alerta de {event_type.label_es} en "{field_name}": {probability}% de probabilidad '
        f"para el {day} (tu umbral: {threshold}%)."
    )


def _format_last_error(exc: BaseException, phone_number: str) -> str:
    """`Tipo: mensaje`, con todas las variantes del teléfono enmascaradas y truncado a 1000.

    Un proveedor puede incluir el número en el error: nunca se persiste completo (SEC-1).
    """
    message = f"{type(exc).__name__}: {exc}"
    return mask_phone_in_text(message, phone_number)[:LAST_ERROR_MAX_LENGTH]


def _backoff_seconds(attempts: int, backoff_base_seconds: int) -> int:
    """Backoff exponencial tras `attempts` fallos previos: base, base*2, base*4, ..."""
    return backoff_base_seconds * int(2**attempts)


def _claim_statement(
    now: datetime, batch_size: int, max_forecast_age_hours: int
) -> Select[tuple[Notification, str, str, datetime | None, bool, int, datetime | None, int | None]]:
    """SELECT del lote vencido, con teléfono, campo y estado actual de la alerta (sin N+1).

    El predicado de estado es una igualdad (`status == PENDING`) para que el planner pueda
    usar el índice parcial `ix_notifications_pending_due ... WHERE status = 'pending'`.
    `FOR UPDATE OF notifications` lockea solo la cola: `alerts`/`fields`/`users` no se lockean.
    Se traen `deleted_at`, `is_active` y `threshold` de la alerta para la re-validación (D12)
    y `issued_at`/`probability` del pronóstico vigente para la de frescura (S11, D14) y de
    umbral (S12a SEC-5), todo en la misma transacción del claim. El LEFT JOIN no cambia la fila
    base: la clave natural `(field_id, forecast_date, event_type)` es única en
    `weather_forecasts`.
    """
    statement = (
        select(
            Notification,
            User.phone_number,
            Field.name,
            Alert.deleted_at,
            Alert.is_active,
            Alert.threshold,
            WeatherForecast.issued_at,
            WeatherForecast.probability,
        )
        .join(Alert, Alert.id == Notification.alert_id)
        .join(Field, Field.id == Alert.field_id)
        .join(User, User.id == Field.user_id)
        .outerjoin(
            WeatherForecast,
            (WeatherForecast.field_id == Alert.field_id)
            & (WeatherForecast.event_type == Notification.event_type)
            & (WeatherForecast.forecast_date == Notification.forecast_date),
        )
        .where(
            Notification.status == NotificationStatus.PENDING,
            Notification.next_attempt_at <= now,
        )
        .order_by(Notification.next_attempt_at, Notification.id)
        .limit(batch_size)
        .with_for_update(of=Notification, skip_locked=True)
    )
    # SQLAlchemy tipa `issued_at`/`probability` como no nulos; el `LEFT JOIN` los vuelve
    # opcionales cuando el pronóstico vigente ya no existe (CR-2).
    return statement  # type: ignore[return-value]


def _cancellation_reason(
    *,
    alert_deleted_at: datetime | None,
    alert_is_active: bool,
    alert_threshold: int,
    forecast_probability: int | None,
    forecast_issued_at: datetime | None,
    stale_forecast_before: datetime,
) -> str | None:
    """Motivo por el que una notificación reclamada ya no corresponde enviarse (D12/S11/S12a).

    El orden importa: una alerta borrada gana sobre una inactiva, una inactiva gana sobre el
    umbral y el estado de la alerta gana sobre la frescura (así `last_error` es determinístico).
    El umbral se compara contra la probabilidad del **pronóstico vigente** (S12a SEC-5), no
    contra el snapshot de la notificación: si el pronóstico bajó, el aviso ya no corresponde.
    Un pronóstico ausente o emitido antes de `now - max_forecast_age_hours` ya no sirve para
    el aviso: la notificación se cancela como `stale_forecast` sin enviarse.
    """
    if alert_deleted_at is not None:
        return "alert_deleted"
    if not alert_is_active:
        return "alert_inactive"
    if (
        forecast_probability is not None
        and forecast_issued_at is not None
        and forecast_probability < alert_threshold
    ):
        return "below_current_threshold"
    if forecast_issued_at is None or forecast_issued_at < stale_forecast_before:
        return "stale_forecast"
    return None


async def _claim_batch(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
    batch_size: int,
    lease_seconds: int,
    max_forecast_age_hours: int,
) -> tuple[list[ClaimedNotification], int]:
    """Transacción 1: reclama el lote con `SKIP LOCKED`, valida y arrienda; commitea.

    Además del lease, re-verifica contra la alerta **actual** (S9, D12), contra la probabilidad
    del **pronóstico vigente** (S12a SEC-5) y contra su frescura (S11, D14): las notificaciones
    cuya alerta está borrada o inactiva, cuyo pronóstico vigente bajó del umbral actual, o cuyo
    pronóstico no existe / venció (`now - max_forecast_age_hours`) pasan a `cancelled` con su
    motivo en `last_error` y no se envían. Todo ocurre en esta misma transacción corta.

    El commit libera los locks: nadie espera a la red con una transacción abierta.
    `lease_until = now + lease_seconds` se calcula una sola vez por lote; ese valor exacto es el
    token de propiedad que después exige la transacción de resultados. `now` debe ser la hora
    real del claim: reusar un `now` viejo haría nacer el lease ya vencido.
    """
    lease_until = now + timedelta(seconds=lease_seconds)
    stale_forecast_before = now - timedelta(hours=max_forecast_age_hours)
    claimed: list[ClaimedNotification] = []
    cancellations: dict[str, list[int]] = {}
    async with sessionmaker() as session:
        rows = (
            await session.execute(_claim_statement(now, batch_size, max_forecast_age_hours))
        ).all()
        for (
            notification,
            phone_number,
            field_name,
            alert_deleted_at,
            alert_is_active,
            alert_threshold,
            forecast_issued_at,
            forecast_probability,
        ) in rows:
            reason = _cancellation_reason(
                alert_deleted_at=alert_deleted_at,
                alert_is_active=alert_is_active,
                alert_threshold=alert_threshold,
                forecast_probability=forecast_probability,
                forecast_issued_at=forecast_issued_at,
                stale_forecast_before=stale_forecast_before,
            )
            if reason is not None:
                cancellations.setdefault(reason, []).append(notification.id)
                continue
            claimed.append(
                ClaimedNotification(
                    id=notification.id,
                    forecast_date=notification.forecast_date,
                    event_type=notification.event_type,
                    probability=notification.probability,
                    threshold=notification.threshold,
                    attempts=notification.attempts,
                    phone_number=phone_number,
                    field_name=field_name,
                    lease_until=lease_until,
                )
            )

        for reason, notification_ids in cancellations.items():
            await session.execute(
                update(Notification)
                .where(
                    Notification.id.in_(notification_ids),
                    Notification.status == NotificationStatus.PENDING,
                )
                .values(
                    status=NotificationStatus.CANCELLED,
                    last_error=reason,
                    updated_at=func.now(),
                )
                .execution_options(synchronize_session=False)
            )
        if claimed:
            await session.execute(
                update(Notification)
                .where(Notification.id.in_([item.id for item in claimed]))
                .values(next_attempt_at=lease_until)
                .execution_options(synchronize_session=False)
            )
        await session.commit()
    return claimed, sum(len(ids) for ids in cancellations.values())


async def _send_batch(
    notifier: Notifier,
    claimed: list[ClaimedNotification],
    *,
    timeout_seconds: float,
    concurrency: int,
) -> list[_SendOutcome]:
    """Envía el lote fuera de transacción, con a lo sumo `concurrency` envíos en vuelo.

    Un error cualquiera (timeout incluido) queda como resultado fallido y nunca corta el lote;
    `CancelledError` se propaga para que el shutdown sea inmediato (las filas quedan arrendadas).
    """
    semaphore = asyncio.Semaphore(concurrency)

    async def _send_one(item: ClaimedNotification) -> _SendOutcome:
        message = NotificationMessage(
            notification_id=item.id,
            phone_number=item.phone_number,
            text=build_message_text(
                event_type=item.event_type,
                field_name=item.field_name,
                probability=item.probability,
                threshold=item.threshold,
                forecast_date=item.forecast_date,
            ),
        )
        async with semaphore:
            try:
                await asyncio.wait_for(notifier.send(message), timeout=timeout_seconds)
            except Exception as exc:  # un fallo cualquiera (timeout incluido) no corta el lote
                return _SendOutcome(item, _format_last_error(exc, item.phone_number))
        return _SendOutcome(item, None)

    return list(await asyncio.gather(*(_send_one(item) for item in claimed)))


async def _record_outcomes(
    sessionmaker: async_sessionmaker[AsyncSession],
    outcomes: list[_SendOutcome],
    *,
    clock: Callable[[], datetime],
    max_attempts: int,
    backoff_base_seconds: int,
) -> DispatchResult:
    """Transacción 2: persiste cada resultado solo si el lote sigue siendo el dueño del lease.

    La guarda `next_attempt_at == lease_until` es el token de propiedad: si el lease venció y otra
    réplica reclamó la fila (u otro worker ya registró el intento), el UPDATE afecta 0 filas y se
    ignora. Así un mismo intento nunca se cuenta dos veces. `clock()` da la hora real al registrar
    (`sent_at` y backoff).
    """
    recorded_at = clock()
    sent = retried = failed = 0
    async with sessionmaker() as session:
        for outcome in outcomes:
            item = outcome.claimed
            statement = (
                update(Notification)
                .where(
                    Notification.id == item.id,
                    Notification.status == NotificationStatus.PENDING,
                    Notification.next_attempt_at == item.lease_until,
                )
                .execution_options(synchronize_session=False)
            )
            if outcome.error is None:
                statement = statement.values(
                    status=NotificationStatus.SENT,
                    sent_at=recorded_at,
                    attempts=Notification.attempts + 1,
                    last_error=None,
                )
                bucket = "sent"
            elif item.attempts + 1 >= max_attempts:
                statement = statement.values(
                    status=NotificationStatus.FAILED,
                    attempts=Notification.attempts + 1,
                    last_error=outcome.error,
                )
                bucket = "failed"
            else:
                statement = statement.values(
                    attempts=Notification.attempts + 1,
                    last_error=outcome.error,
                    next_attempt_at=recorded_at
                    + timedelta(seconds=_backoff_seconds(item.attempts, backoff_base_seconds)),
                )
                bucket = "retried"

            updated = (
                await session.execute(statement.returning(Notification.id))
            ).scalar_one_or_none()
            if updated is None:
                continue
            if bucket == "sent":
                sent += 1
            elif bucket == "failed":
                failed += 1
            else:
                retried += 1
        await session.commit()
    return DispatchResult(sent=sent, retried=retried, failed=failed)


async def dispatch_pending(
    sessionmaker: async_sessionmaker[AsyncSession],
    notifier: Notifier,
    *,
    now: datetime | None = None,
    clock: Callable[[], datetime] = _utc_now,
    batch_size: int,
    max_attempts: int,
    backoff_base_seconds: int,
    timeout_seconds: float,
    concurrency: int,
    lease_seconds: int,
    max_forecast_age_hours: int,
) -> DispatchResult:
    """Envía hasta `batch_size` notificaciones pendientes vencidas (patrón lease).

    1. Claim + lease + validación contra la alerta actual, la probabilidad del pronóstico
       vigente y su frescura en una transacción corta: las que ya no corresponden (alerta
       borrada/inactiva, pronóstico bajo el umbral actual o `stale_forecast`) pasan a
       `cancelled` sin enviarse (se cuentan en `cancelled`).
    2. Envío en paralelo acotado (`concurrency`) con timeout por mensaje, sin transacción.
    3. Resultados en otra transacción corta: `sent`, reintento con backoff exponencial o
       `failed` al agotar `max_attempts`.

    `now` es la hora del claim y debe ser la hora real de ese momento: **nunca** se reutiliza
    entre lotes, porque el lease se calcula como `now + lease_seconds` y uno viejo nacería
    vencido. Si es `None` se toma `clock()`. `clock()` es además la hora real con la que se
    registran `sent_at` y el backoff. `max_forecast_age_hours` define la frescura mínima del
    pronóstico vigente al momento del claim (S11, D14). `CancelledError` se propaga (shutdown);
    las filas arrendadas se reintentan al vencer el lease.
    """
    claim_now = now if now is not None else clock()
    started = time.perf_counter()
    claimed, cancelled = await _claim_batch(
        sessionmaker,
        now=claim_now,
        batch_size=batch_size,
        lease_seconds=lease_seconds,
        max_forecast_age_hours=max_forecast_age_hours,
    )
    if claimed:
        outcomes = await _send_batch(
            notifier,
            claimed,
            timeout_seconds=timeout_seconds,
            concurrency=concurrency,
        )
        recorded = await _record_outcomes(
            sessionmaker,
            outcomes,
            clock=clock,
            max_attempts=max_attempts,
            backoff_base_seconds=backoff_base_seconds,
        )
        result = DispatchResult(
            sent=recorded.sent,
            retried=recorded.retried,
            failed=recorded.failed,
            cancelled=cancelled,
        )
    else:
        result = DispatchResult(sent=0, retried=0, failed=0, cancelled=cancelled)

    duration_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "dispatch_done sent=%d retried=%d failed=%d cancelled=%d duration_ms=%d",
        result.sent,
        result.retried,
        result.failed,
        result.cancelled,
        duration_ms,
    )
    return result
