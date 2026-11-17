"""Casos de uso de alertas. Ver docs/API.md §alerts.

`alerts` no tiene `user_id`: el dueño se deriva de `fields.user_id`, así que toda query
hace JOIN con `fields` y filtra por el usuario autenticado. Una alerta (o un campo) de otro
usuario se comporta como inexistente (NotFoundError, nunca 403) para no filtrar IDs.
"""

from collections.abc import Sequence

from sqlalchemy import Select, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import ConflictError, NotFoundError
from app.jobs.evaluator import EVALUATOR_LOCK_KEY
from app.models import Alert, AlertDayState, EventType, Field, Notification, NotificationStatus
from app.schemas.alert import AlertCreate, AlertUpdate
from app.schemas.common import BIGINT_MAX
from app.services._db_errors import is_unique_violation
from app.services.fields import get_field

#: Detalle del 409 (docs/API.md §alerts): unicidad solo entre alertas no borradas.
DUPLICATE_ALERT_DETAIL = "An alert with this field, event type and threshold already exists"

#: Detalle del 409 (docs/API.md §alerts, "Límite"): tope de alertas vivas por campo.
ALERT_LIMIT_DETAIL = "Alert limit reached for this field"


def _owned_alerts(user_id: int, *, include_deleted: bool = False) -> Select[tuple[Alert]]:
    """Query base de las alertas del usuario (ownership vía JOIN con `fields`).

    Por defecto excluye las alertas borradas lógicamente: no se listan, no se leen, no se
    modifican y el evaluador tampoco las considera. `include_deleted` solo lo usa el DELETE,
    que es idempotente sobre una alerta propia ya borrada.
    """
    query = select(Alert).join(Field, Field.id == Alert.field_id).where(Field.user_id == user_id)
    if not include_deleted:
        query = query.where(Alert.deleted_at.is_(None))
    return query


async def create_alert(
    session: AsyncSession, user_id: int, data: AlertCreate, *, commit: bool = True
) -> Alert:
    """Crea una alerta del usuario.

    Campo ajeno/inexistente -> 404; duplicado -> 409; campo con
    `max_alerts_per_field` alertas vivas -> 409 (docs/API.md §alerts, "Límite").

    El duplicado se detecta con `flush()` (índice único parcial, sqlstate 23505). Con
    `commit=False` la fila queda en la transacción abierta para la idempotencia
    (docs/API.md §Idempotency-Key).
    """
    # Valida rango BIGINT y ownership: un id fuera de rango no se consulta (sería un 500).
    field = await get_field(session, user_id, data.field_id)
    # Lock de la fila del campo dentro de esta transacción: el conteo y el INSERT quedan
    # serializados contra otros POST del mismo campo. Sin el lock, dos requests en el
    # límite podrían contar N-1 e insertar ambos (N+1 alertas vivas).
    await session.execute(select(Field.id).where(Field.id == field.id).with_for_update())
    live_alerts = (
        await session.execute(
            select(func.count())
            .select_from(Alert)
            .where(Alert.field_id == field.id, Alert.deleted_at.is_(None))
        )
    ).scalar_one()
    if live_alerts >= get_settings().max_alerts_per_field:
        await session.rollback()
        raise ConflictError(ALERT_LIMIT_DETAIL)
    alert = Alert(field_id=field.id, event_type=data.event_type, threshold=data.threshold)
    session.add(alert)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        if not is_unique_violation(exc):
            raise
        # Índice único parcial (field_id, event_type, threshold) WHERE deleted_at IS NULL:
        # detectado con la constraint, no con un SELECT previo. Una alerta borrada se puede
        # recrear con el mismo umbral.
        raise ConflictError(DUPLICATE_ALERT_DETAIL) from None
    if commit:
        await session.commit()
    return alert


async def list_alerts(
    session: AsyncSession,
    user_id: int,
    *,
    field_id: int | None,
    event_type: EventType | None,
    is_active: bool | None,
    limit: int,
    offset: int,
) -> Sequence[Alert]:
    """Lista las alertas del usuario, con filtros opcionales, ordenadas por `id` ASC."""
    query = _owned_alerts(user_id)
    if field_id is not None:
        query = query.where(Alert.field_id == field_id)
    if event_type is not None:
        query = query.where(Alert.event_type == event_type)
    if is_active is not None:
        query = query.where(Alert.is_active.is_(is_active))
    query = query.order_by(Alert.id).limit(limit).offset(offset)
    result = await session.execute(query)
    return result.scalars().all()


async def get_alert(session: AsyncSession, user_id: int, alert_id: int) -> Alert:
    """Devuelve la alerta del usuario o lanza `NotFoundError` si no existe / no es suya."""
    # Un id fuera del rango BIGINT no puede existir: ni siquiera se consulta (sería un 500).
    if not 1 <= alert_id <= BIGINT_MAX:
        raise NotFoundError("Alert not found")
    result = await session.execute(_owned_alerts(user_id).where(Alert.id == alert_id))
    alert = result.scalar_one_or_none()
    if alert is None:
        raise NotFoundError("Alert not found")
    return alert


def _requires_rearm(alert: Alert, changes: dict[str, object]) -> bool:
    """¿El PATCH re-evalúa la alerta desde cero? (docs/JOBS.md §Episodios, S12b).

    Solo si el `threshold` cambia a otro valor o si `is_active` pasa de `false` a `true`.
    Pausar (o mandar el mismo umbral / `is_active` que ya era `true`) no toca el estado.
    """
    threshold_changed = "threshold" in changes and changes["threshold"] != alert.threshold
    reactivated = changes.get("is_active") is True and not alert.is_active
    return threshold_changed or reactivated


async def _rearm_alert(session: AsyncSession, alert_id: int) -> None:
    """Re-arma los días de la alerta y cancela sus notificaciones `pending` no arrendadas.

    En la misma transacción del UPDATE de la alerta y en este orden (JOBS.md §Episodios):
    `alert_day_states.armed = true` (= `reset` en `app/jobs/episodes.py`) y, después,
    `pending -> cancelled` con `last_error = 'alert_reset'`. Como en el DELETE de S9, las
    arrendadas (`next_attempt_at` en el futuro) no se tocan: el dispatcher registra su
    resultado real.
    """
    await session.execute(
        update(AlertDayState)
        .where(AlertDayState.alert_id == alert_id)
        .values(armed=True, updated_at=func.now())
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        update(Notification)
        .where(
            Notification.alert_id == alert_id,
            Notification.status == NotificationStatus.PENDING,
            Notification.next_attempt_at <= func.now(),
        )
        .values(
            status=NotificationStatus.CANCELLED,
            last_error="alert_reset",
            updated_at=func.now(),
        )
        .execution_options(synchronize_session=False)
    )


async def update_alert(
    session: AsyncSession, user_id: int, alert_id: int, data: AlertUpdate
) -> Alert:
    """Actualiza solo los campos presentes en el PATCH. Commit en el service.

    La escritura es un `UPDATE ... WHERE id = :id AND deleted_at IS NULL RETURNING ...`:
    si un DELETE concurrente commitea entre la lectura de ownership y la escritura, el
    UPDATE afecta 0 filas y se responde 404 (nunca se modifica una alerta ya borrada).
    Cambiar el `threshold` a uno que ya usa otra alerta viva del mismo (campo, evento)
    viola el índice único parcial -> 409.

    Si el PATCH cambia el umbral o reactiva la alerta (`is_active` false -> true), sus días
    se re-arman y sus notificaciones `pending` no arrendadas pasan a `cancelled`
    (`alert_reset`), en la misma transacción (S12b, D1). El próximo ciclo re-avisa los días
    que siguen arriba del umbral vigente.

    Antes de leer la alerta toma `pg_advisory_xact_lock_shared(EVALUATOR_LOCK_KEY)`
    (S12b SEC-1/CR-1): el PATCH espera a que termine una evaluación en curso y el evaluador
    (lock exclusivo) espera a los PATCH abiertos, así ninguno ve el umbral de la otra
    transacción ni re-arma después de que el otro canceló. Al ser compartido, dos PATCH
    concurrentes no se bloquean entre sí. `AlertUpdate` solo expone `threshold` e
    `is_active`, así que todo PATCH toma el lock.
    """
    changes = data.model_dump(exclude_unset=True)
    # Paso 0 de docs/JOBS.md §Episodios ("Re-armado por cambio de alerta"): el lock va
    # **antes** de leer/actualizar la alerta, para que snapshot, re-armado y cancelación
    # sean atómicos respecto de la evaluación.
    await session.execute(select(func.pg_advisory_xact_lock_shared(EVALUATOR_LOCK_KEY)))
    # Valida ownership y existencia (404) antes de escribir; trae los valores previos que
    # deciden si corresponde re-armar.
    current = await get_alert(session, user_id, alert_id)
    rearm = _requires_rearm(current, changes)
    try:
        updated = (
            await session.execute(
                update(Alert)
                .where(Alert.id == alert_id, Alert.deleted_at.is_(None))
                .values(**changes)
                .returning(Alert)
                .execution_options(synchronize_session=False, populate_existing=True)
            )
        ).scalar_one_or_none()
        if updated is None:
            await session.rollback()
            raise NotFoundError("Alert not found")
        if rearm:
            await _rearm_alert(session, alert_id)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if not is_unique_violation(exc):
            raise
        raise ConflictError(DUPLICATE_ALERT_DETAIL) from None
    return updated


async def delete_alert(session: AsyncSession, user_id: int, alert_id: int) -> None:
    """Borra lógicamente la alerta y cancela sus notificaciones pendientes no arrendadas.

    Borrado lógico e idempotente (D11): marca `deleted_at` y, en la misma transacción, pasa
    las notificaciones `pending` de la alerta a `cancelled` con `last_error='alert_deleted'`.
    Solo se cancelan las que ya vencieron (`next_attempt_at <= now()`); las que el dispatcher
    tiene arrendadas (`next_attempt_at` en el futuro) se dejan intactas para que registre su
    resultado real (`sent`/`failed`): un mensaje que salió nunca queda como `cancelled`. Si el
    worker muere sin registrar, al vencer el lease el próximo claim la cancela por
    `alert_deleted` (validación D12). El historial (`sent`/`failed`/`cancelled`) se conserva.
    Repetir el DELETE de una alerta propia ya borrada sigue siendo 204; solo una alerta
    inexistente o ajena es 404.

    Se bloquea la fila (`FOR UPDATE OF alerts`) para que dos DELETEs concurrentes no se pisen
    y para que el UPDATE de notificaciones sea atómico con el marcado.
    """
    if not 1 <= alert_id <= BIGINT_MAX:
        raise NotFoundError("Alert not found")
    result = await session.execute(
        _owned_alerts(user_id, include_deleted=True)
        .where(Alert.id == alert_id)
        .with_for_update(of=Alert)
        # Una sesión puede tener la instancia cacheada con `deleted_at=None` (p. ej. repetir
        # el DELETE en el mismo request de test): se relee el estado real bajo el lock.
        .execution_options(populate_existing=True)
    )
    alert = result.scalar_one_or_none()
    if alert is None:
        raise NotFoundError("Alert not found")
    if alert.deleted_at is not None:
        # Idempotencia (D11): ya estaba borrada; no se mueve el `deleted_at` original.
        return

    await session.execute(
        update(Alert)
        .where(Alert.id == alert.id, Alert.deleted_at.is_(None))
        .values(deleted_at=func.now(), updated_at=func.now())
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        update(Notification)
        .where(
            Notification.alert_id == alert.id,
            Notification.status == NotificationStatus.PENDING,
            # Solo las no arrendadas: un lease vigente tiene `next_attempt_at` en el futuro.
            Notification.next_attempt_at <= func.now(),
        )
        .values(
            status=NotificationStatus.CANCELLED,
            last_error="alert_deleted",
            updated_at=func.now(),
        )
        .execution_options(synchronize_session=False)
    )
    await session.commit()
