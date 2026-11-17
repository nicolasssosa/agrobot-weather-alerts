"""Despacho de notificaciones pendientes (outbox, patrón lease).

Spec: docs/JOBS.md §Dispatcher y §Envío agrupado por usuario y horario de silencio (S13, D15).
Regla de oro: nunca se espera a la red con una transacción abierta. El lote se reclama y se
arrienda (`next_attempt_at = now + lease`) en una transacción corta; los envíos corren fuera de
transacción, en paralelo acotado; y los resultados se persisten en otra transacción corta. Las
filas reclamadas quedan `pending` pero invisibles para los demás workers hasta que vence el
lease, así que un proceso que muere entre el envío y el commit no deja locks: la notificación se
reenvía sola al vencer el lease (semántica at-least-once, D10).

Desde S13 el mensaje es **uno por usuario y lote**: el claim lockea la fila del usuario
(`FOR NO KEY UPDATE SKIP LOCKED`) para que dos réplicas nunca manden dos mensajes al mismo
usuario, toma a lo sumo `digest_max_items` notificaciones suyas y respeta su horario de silencio.
Cada notificación conserva su estado individual (`sent`, reintento, `failed`, `cancelled`): lo
único compartido es el envío.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import Select, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.pii import mask_phone_in_text
from app.jobs.quiet_hours import quiet_until
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

#: Claim de los usuarios del lote (docs/JOBS.md §S13). `FOR NO KEY UPDATE` (no `FOR UPDATE`)
#: para no bloquear los INSERT de `fields` que validan la FK (`FOR KEY SHARE`). SQLAlchemy
#: 2.0 no expone `FOR NO KEY UPDATE`, de ahí el `text()` con binds.
_USERS_CLAIM_SQL = text(
    """
    SELECT u.id, u.phone_number, u.timezone, u.quiet_hours_start, u.quiet_hours_end
    FROM users u
    WHERE u.id IN (
        SELECT f.user_id
        FROM notifications n
        JOIN alerts a ON a.id = n.alert_id
        JOIN fields f ON f.id = a.field_id
        WHERE n.status = 'pending' AND n.next_attempt_at <= :now
        GROUP BY f.user_id
        ORDER BY min(n.next_attempt_at), f.user_id
        LIMIT :batch_size
    )
    ORDER BY u.id
    FOR NO KEY UPDATE SKIP LOCKED
    """
)


def _utc_now() -> datetime:
    """Reloj real por defecto; inyectable en tests (nunca se usa en ellos el reloj real)."""
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """Conteo del lote: notificaciones enviadas, reintentadas, agotadas, canceladas y diferidas."""

    sent: int
    retried: int
    failed: int
    #: Notificaciones que ya no correspondía enviar (alerta borrada/inactiva/día pasado/umbral
    #: o pronóstico vencido).
    cancelled: int = 0
    #: Notificaciones diferidas por el horario de silencio de su usuario (S13).
    deferred: int = 0
    #: Usuarios procesados en el lote (con envío, diferidos o todo cancelado).
    users: int = 0


@dataclass(frozen=True, slots=True)
class ClaimedUser:
    """Snapshot plano del usuario lockeado en el claim (leído antes de cerrar la sesión)."""

    id: int
    phone_number: str
    timezone: str
    quiet_hours_start: dt_time | None
    quiet_hours_end: dt_time | None


@dataclass(frozen=True, slots=True)
class ClaimedNotification:
    """Snapshot plano de una notificación reclamada, leído antes de cerrar la sesión."""

    id: int
    user_id: int
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


@dataclass(frozen=True, slots=True)
class _ClaimBatch:
    """Resultado de la transacción 1: lo arrendado, lo diferido, lo cancelado y los usuarios."""

    claimed: list[ClaimedNotification]
    cancelled: int
    deferred: int
    users: int


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


def _capitalized_label(event_type: EventType) -> str:
    """`event_type.label_es` con la primera letra en mayúscula (docs/JOBS.md §S13)."""
    label = event_type.label_es
    return label[:1].upper() + label[1:]


def build_digest_text(items: Sequence[ClaimedNotification]) -> str:
    """Arma el texto exacto del digest (docs/JOBS.md §Texto del digest, S13).

    Líneas ordenadas por `(forecast_date, field_name, event_type.value, threshold, id)`,
    separadas por `\\n` y sin `\\n` final. Menos de 2 items es un error de uso.
    """
    if len(items) < 2:
        raise ValueError("build_digest_text requires at least 2 notifications")
    ordered = sorted(
        items,
        key=lambda item: (
            item.forecast_date,
            item.field_name,
            item.event_type.value,
            item.threshold,
            item.id,
        ),
    )
    lines = [f"⚠️ Tenés {len(ordered)} alertas climáticas:"]
    for item in ordered:
        day = item.forecast_date.strftime("%d/%m/%Y")
        lines.append(
            f'• {_capitalized_label(item.event_type)} en "{item.field_name}": '
            f"{item.probability}% de probabilidad para el {day} "
            f"(tu umbral: {item.threshold}%)."
        )
    return "\n".join(lines)


def _message_text(items: Sequence[ClaimedNotification]) -> str:
    """Texto del mensaje del usuario: el de siempre con 1 aviso, el digest con 2 o más."""
    if len(items) == 1:
        item = items[0]
        return build_message_text(
            event_type=item.event_type,
            field_name=item.field_name,
            probability=item.probability,
            threshold=item.threshold,
            forecast_date=item.forecast_date,
        )
    return build_digest_text(items)


def _format_last_error(exc: BaseException, phone_number: str) -> str:
    """`Tipo: mensaje`, con todas las variantes del teléfono enmascaradas y truncado a 1000.

    Un proveedor puede incluir el número en el error: nunca se persiste completo (SEC-1).
    """
    message = f"{type(exc).__name__}: {exc}"
    return mask_phone_in_text(message, phone_number)[:LAST_ERROR_MAX_LENGTH]


def _backoff_seconds(attempts: int, backoff_base_seconds: int) -> int:
    """Backoff exponencial tras `attempts` fallos previos: base, base*2, base*4, ..."""
    return backoff_base_seconds * int(2**attempts)


def _ranked_due_ids(
    now: datetime, user_ids: Sequence[int], digest_max_items: int
) -> Select[tuple[int]]:
    """Ids de las notificaciones más antiguas de cada usuario, a lo sumo `digest_max_items`.

    `row_number()` no puede convivir con `FOR UPDATE`, de ahí la subconsulta de ids: el
    excedente queda `pending` y vencido, y sale en el lote siguiente (el worker sigue mientras
    el lote venga lleno).
    """
    ranked = (
        select(
            Notification.id.label("id"),
            func.row_number()
            .over(
                partition_by=Field.user_id,
                order_by=(Notification.next_attempt_at, Notification.id),
            )
            .label("row_number"),
        )
        .join(Alert, Alert.id == Notification.alert_id)
        .join(Field, Field.id == Alert.field_id)
        .where(
            Notification.status == NotificationStatus.PENDING,
            Notification.next_attempt_at <= now,
            Field.user_id.in_(user_ids),
        )
        .subquery()
    )
    return select(ranked.c.id).where(ranked.c.row_number <= digest_max_items)


def _claim_statement(
    now: datetime, *, user_ids: Sequence[int], digest_max_items: int
) -> Select[
    tuple[
        Notification,
        str,
        str,
        datetime | None,
        bool,
        int,
        datetime | None,
        int | None,
        int,
        str,
    ]
]:
    """SELECT de las notificaciones de los usuarios lockeados, con su contexto (sin N+1).

    Es el mismo SELECT de validación de siempre (teléfono, campo, estado actual de la alerta,
    pronóstico vigente) más `user_id` y `timezone` del campo, limitado a `digest_max_items` por
    usuario y con `FOR UPDATE OF notifications` (los usuarios ya están lockeados aparte).
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
            Field.user_id,
            Field.timezone,
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
        .where(Notification.id.in_(_ranked_due_ids(now, user_ids, digest_max_items)))
        .order_by(Notification.next_attempt_at, Notification.id)
        .with_for_update(of=Notification, skip_locked=True)
    )
    # SQLAlchemy tipa `issued_at`/`probability` como no nulos; el `LEFT JOIN` los vuelve
    # opcionales cuando el pronóstico vigente ya no existe (CR-2).
    return statement  # type: ignore[return-value]


def _local_today(now: datetime, timezone: str) -> date | None:
    """Fecha local del campo a `now`; `None` si `zoneinfo` no reconoce la zona.

    Una zona inválida saltea solo el chequeo de día pasado (docs/JOBS.md §S13): el campo no
    puede abortar el lote por un dato mal cargado.
    """
    try:
        return now.astimezone(ZoneInfo(timezone)).date()
    except ZoneInfoNotFoundError:
        logger.warning("invalid_field_timezone timezone=%s", timezone)
        return None


def _cancellation_reason(
    *,
    alert_deleted_at: datetime | None,
    alert_is_active: bool,
    forecast_date: date,
    local_today: date | None,
    alert_threshold: int,
    forecast_probability: int | None,
    forecast_issued_at: datetime | None,
    stale_forecast_before: datetime,
) -> str | None:
    """Motivo por el que una notificación reclamada ya no corresponde enviarse (D12/S11/S13).

    El orden importa (docs/JOBS.md §S13): `alert_deleted` → `alert_inactive` →
    `forecast_date_passed` → `below_current_threshold` → `stale_forecast`, así `last_error`
    es determinístico. El día pasado se mide en la zona del campo ("hoy" local); una zona
    desconocida (`local_today is None`) saltea solo ese chequeo. El umbral se compara contra la
    probabilidad del **pronóstico vigente** (S12a SEC-5), no contra el snapshot.
    """
    if alert_deleted_at is not None:
        return "alert_deleted"
    if not alert_is_active:
        return "alert_inactive"
    if local_today is not None and forecast_date < local_today:
        return "forecast_date_passed"
    if (
        forecast_probability is not None
        and forecast_issued_at is not None
        and forecast_probability < alert_threshold
    ):
        return "below_current_threshold"
    if forecast_issued_at is None or forecast_issued_at < stale_forecast_before:
        return "stale_forecast"
    return None


def _quiet_until_for(user: ClaimedUser, now: datetime) -> datetime | None:
    """Fin del silencio del usuario (o `None` si no está en silencio).

    Una zona que `zoneinfo` no reconoce no puede diferir nada: se loguea
    `invalid_user_timezone` y se envía como si no tuviera silencio (docs/JOBS.md §S13).
    """
    try:
        return quiet_until(
            now,
            timezone=user.timezone,
            start=user.quiet_hours_start,
            end=user.quiet_hours_end,
        )
    except ZoneInfoNotFoundError:
        logger.warning("invalid_user_timezone user_id=%d", user.id)
        return None


async def _lock_users(
    session: AsyncSession, *, now: datetime, batch_size: int
) -> list[ClaimedUser]:
    """Lockea los usuarios con notificaciones vencidas: como máximo `batch_size` por lote."""
    rows = (await session.execute(_USERS_CLAIM_SQL, {"now": now, "batch_size": batch_size})).all()
    return [
        ClaimedUser(
            id=row.id,
            phone_number=row.phone_number,
            timezone=row.timezone,
            quiet_hours_start=row.quiet_hours_start,
            quiet_hours_end=row.quiet_hours_end,
        )
        for row in rows
    ]


async def _claim_batch(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
    batch_size: int,
    lease_seconds: int,
    max_forecast_age_hours: int,
    digest_max_items: int,
) -> _ClaimBatch:
    """Transacción 1: reclama hasta `batch_size` usuarios, valida, difiere y arrienda; commitea.

    1. Lockea la fila de cada usuario del lote (`FOR NO KEY UPDATE SKIP LOCKED`): es lo que
       garantiza un solo mensaje por usuario con varias réplicas.
    2. Reclama sus notificaciones vencidas (a lo sumo `digest_max_items` por usuario) y
       re-verifica contra la alerta **actual** (S9, D12), la probabilidad del **pronóstico
       vigente** (S12a SEC-5), su frescura (S11, D14) y el día pronosticado ya pasado (S13).
       Las que no corresponden pasan a `cancelled` con su motivo en `last_error`.
    3. Si el usuario está en horario de silencio, sus notificaciones válidas quedan `pending`
       con `next_attempt_at` = fin del silencio (sin tocar `attempts` ni `last_error`) y se
       cuentan en `deferred`; si no, se arriendan (`next_attempt_at = lease_until`).

    El commit libera los locks: nadie espera a la red con una transacción abierta.
    `lease_until = now + lease_seconds` se calcula una sola vez por lote; ese valor exacto es el
    token de propiedad que después exige la transacción de resultados. `now` debe ser la hora
    real del claim: reusar un `now` viejo haría nacer el lease ya vencido.
    """
    lease_until = now + timedelta(seconds=lease_seconds)
    stale_forecast_before = now - timedelta(hours=max_forecast_age_hours)
    claimed: list[ClaimedNotification] = []
    cancellations: dict[str, list[int]] = {}
    deferred_ids: dict[int, tuple[datetime, list[int]]] = {}
    processed_user_ids: set[int] = set()
    #: "Hoy" local por zona: se calcula una vez por zona y lote (una zona inválida loguea una vez).
    local_today_by_timezone: dict[str, date | None] = {}

    async with sessionmaker() as session:
        users = await _lock_users(session, now=now, batch_size=batch_size)
        if not users:
            await session.commit()
            return _ClaimBatch(claimed=[], cancelled=0, deferred=0, users=0)
        users_by_id = {user.id: user for user in users}
        rows = (
            await session.execute(
                _claim_statement(
                    now,
                    user_ids=list(users_by_id),
                    digest_max_items=digest_max_items,
                )
            )
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
            user_id,
            field_timezone,
        ) in rows:
            processed_user_ids.add(user_id)
            if field_timezone not in local_today_by_timezone:
                local_today_by_timezone[field_timezone] = _local_today(now, field_timezone)
            reason = _cancellation_reason(
                alert_deleted_at=alert_deleted_at,
                alert_is_active=alert_is_active,
                forecast_date=notification.forecast_date,
                local_today=local_today_by_timezone[field_timezone],
                alert_threshold=alert_threshold,
                forecast_probability=forecast_probability,
                forecast_issued_at=forecast_issued_at,
                stale_forecast_before=stale_forecast_before,
            )
            if reason is not None:
                cancellations.setdefault(reason, []).append(notification.id)
                continue
            user = users_by_id[user_id]
            until = _quiet_until_for(user, now)
            if until is not None:
                # `until` es el mismo para todas las notificaciones del usuario en este claim.
                deferred_ids.setdefault(user_id, (until, []))[1].append(notification.id)
                continue
            claimed.append(
                ClaimedNotification(
                    id=notification.id,
                    user_id=user_id,
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
        for until, notification_ids in deferred_ids.values():
            await session.execute(
                update(Notification)
                .where(
                    Notification.id.in_(notification_ids),
                    Notification.status == NotificationStatus.PENDING,
                )
                .values(next_attempt_at=until)
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

    deferred = sum(len(ids) for _, ids in deferred_ids.values())
    return _ClaimBatch(
        claimed=claimed,
        cancelled=sum(len(ids) for ids in cancellations.values()),
        deferred=deferred,
        users=len(processed_user_ids),
    )


def _group_by_user(claimed: Sequence[ClaimedNotification]) -> list[list[ClaimedNotification]]:
    """Agrupa los claims por usuario, en el orden en que aparecieron en el lote."""
    groups: dict[int, list[ClaimedNotification]] = {}
    for item in claimed:
        groups.setdefault(item.user_id, []).append(item)
    return list(groups.values())


async def _send_batch(
    notifier: Notifier,
    claimed: Sequence[ClaimedNotification],
    *,
    timeout_seconds: float,
    concurrency: int,
) -> list[_SendOutcome]:
    """Envía un mensaje por usuario fuera de transacción, con `concurrency` mensajes en vuelo.

    Un error cualquiera (timeout incluido) es un resultado fallido para **todo el grupo** y
    nunca corta el lote; `CancelledError` se propaga para que el shutdown sea inmediato (las
    filas quedan arrendadas).
    """
    semaphore = asyncio.Semaphore(concurrency)

    async def _send_group(group: list[ClaimedNotification]) -> list[_SendOutcome]:
        ordered = sorted(group, key=lambda item: item.id)
        message = NotificationMessage(
            notification_id=ordered[0].id,
            phone_number=ordered[0].phone_number,
            text=_message_text(ordered),
        )
        async with semaphore:
            try:
                await asyncio.wait_for(notifier.send(message), timeout=timeout_seconds)
            except Exception as exc:  # un fallo cualquiera (timeout incluido) no corta el lote
                error = _format_last_error(exc, ordered[0].phone_number)
                return [_SendOutcome(item, error) for item in ordered]
        return [_SendOutcome(item, None) for item in ordered]

    group_outcomes = await asyncio.gather(
        *(_send_group(group) for group in _group_by_user(claimed))
    )
    return [outcome for outcomes in group_outcomes for outcome in outcomes]


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
    (`sent_at` y backoff). Cada notificación del grupo se actualiza por separado, con **sus
    propios** `attempts`: una puede pasar a `failed` y otra quedar en reintento.
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
    digest_max_items: int,
) -> DispatchResult:
    """Envía un mensaje por usuario para hasta `batch_size` usuarios con avisos vencidos.

    `batch_size` es la cantidad máxima de **usuarios (= mensajes)** por lote; un lote reclama a
    lo sumo `batch_size * digest_max_items` notificaciones.

    1. Claim + lock de usuario + lease + validación en una transacción corta: las notificaciones
       que ya no corresponden (alerta borrada/inactiva, día pronosticado pasado, pronóstico bajo
       el umbral actual o `stale_forecast`) pasan a `cancelled` sin enviarse (se cuentan en
       `cancelled`). Las de un usuario en horario de silencio quedan `pending` hasta el fin del
       silencio (`deferred`, sin tocar `attempts`).
    2. Envío en paralelo acotado (`concurrency`): un mensaje por usuario, con timeout.
    3. Resultados en otra transacción corta, por notificación: `sent`, reintento con backoff
       exponencial o `failed` al agotar `max_attempts`.

    `now` es la hora del claim y debe ser la hora real de ese momento: **nunca** se reutiliza
    entre lotes, porque el lease se calcula como `now + lease_seconds` y uno viejo nacería
    vencido. Si es `None` se toma `clock()`. `clock()` es además la hora real con la que se
    registran `sent_at` y el backoff. `CancelledError` se propaga (shutdown); las filas
    arrendadas se reintentan al vencer el lease.
    """
    claim_now = now if now is not None else clock()
    started = time.perf_counter()
    claim = await _claim_batch(
        sessionmaker,
        now=claim_now,
        batch_size=batch_size,
        lease_seconds=lease_seconds,
        max_forecast_age_hours=max_forecast_age_hours,
        digest_max_items=digest_max_items,
    )
    if claim.claimed:
        outcomes = await _send_batch(
            notifier,
            claim.claimed,
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
            cancelled=claim.cancelled,
            deferred=claim.deferred,
            users=claim.users,
        )
    else:
        result = DispatchResult(
            sent=0,
            retried=0,
            failed=0,
            cancelled=claim.cancelled,
            deferred=claim.deferred,
            users=claim.users,
        )

    duration_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "dispatch_done users=%d sent=%d retried=%d failed=%d cancelled=%d deferred=%d "
        "duration_ms=%d",
        result.users,
        result.sent,
        result.retried,
        result.failed,
        result.cancelled,
        result.deferred,
        duration_ms,
    )
    return result
