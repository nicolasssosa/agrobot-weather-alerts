"""Casos de uso de alertas. Ver docs/API.md §alerts.

`alerts` no tiene `user_id`: el dueño se deriva de `fields.user_id`, así que toda query
hace JOIN con `fields` y filtra por el usuario autenticado. Una alerta (o un campo) de otro
usuario se comporta como inexistente (NotFoundError, nunca 403) para no filtrar IDs.
"""

from collections.abc import Sequence

from sqlalchemy import Select, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import ObjectDeletedError, StaleDataError

from app.core.errors import ConflictError, NotFoundError
from app.models import Alert, EventType, Field
from app.schemas.alert import AlertCreate, AlertUpdate
from app.schemas.common import BIGINT_MAX
from app.services._db_errors import is_unique_violation
from app.services.fields import get_field


def _owned_alerts(user_id: int) -> Select[tuple[Alert]]:
    """Query base de las alertas del usuario (ownership vía JOIN con `fields`)."""
    return select(Alert).join(Field, Field.id == Alert.field_id).where(Field.user_id == user_id)


async def create_alert(session: AsyncSession, user_id: int, data: AlertCreate) -> Alert:
    """Crea una alerta del usuario. Campo ajeno/inexistente -> 404; duplicado -> 409."""
    # Valida rango BIGINT y ownership: un id fuera de rango no se consulta (sería un 500).
    field = await get_field(session, user_id, data.field_id)
    alert = Alert(field_id=field.id, event_type=data.event_type, threshold=data.threshold)
    session.add(alert)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if not is_unique_violation(exc):
            raise
        # UNIQUE (field_id, event_type): detectado con la constraint, no con un SELECT previo.
        raise ConflictError("An alert for this field and event type already exists") from None
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


async def update_alert(
    session: AsyncSession, user_id: int, alert_id: int, data: AlertUpdate
) -> Alert:
    """Actualiza solo los campos presentes en el PATCH. Commit en el service.

    Un DELETE concurrente de la misma alerta (entre el SELECT y el UPDATE) hace que el
    `UPDATE` afecte 0 filas o que el `refresh` ya no encuentre la fila. Ambas carreras se
    traducen en 404, igual que una alerta inexistente.
    """
    alert = await get_alert(session, user_id, alert_id)
    for name, value in data.model_dump(exclude_unset=True).items():
        setattr(alert, name, value)
    try:
        await session.commit()
        # `updated_at` usa `onupdate=func.now()`: el flush expira el atributo y, en async, un
        # acceso implícito durante la serialización fallaría. Refresh explícito para devolverlo.
        await session.refresh(alert)
    except (StaleDataError, ObjectDeletedError):
        await session.rollback()
        raise NotFoundError("Alert not found") from None
    return alert


async def delete_alert(session: AsyncSession, user_id: int, alert_id: int) -> None:
    """Borra la alerta; sus notificaciones caen por el ON DELETE CASCADE de la FK.

    Se usa un DELETE de Core (y no `session.delete`) para no cargar la relación
    `notifications` (declarada `lazy="raise"`): el cascade lo resuelve Postgres.
    """
    alert = await get_alert(session, user_id, alert_id)
    await session.execute(delete(Alert).where(Alert.id == alert.id))
    await session.commit()
