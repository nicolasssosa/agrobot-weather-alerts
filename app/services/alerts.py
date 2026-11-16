"""Casos de uso de alertas. TODO(S4).

async def create_alert(session, user_id: int, data: AlertCreate) -> Alert
    - field_id debe ser del usuario -> si no, NotFoundError("Field not found").
    - (field_id, event_type) duplicado -> ConflictError(
          "An alert for this field and event type already exists").
      Detectarlo capturando IntegrityError (no con SELECT previo: race condition).
async def list_alerts(
    session, user_id: int, *, field_id: int | None, event_type: EventType | None,
    is_active: bool | None, limit: int, offset: int,
) -> Sequence[Alert]
    - Orden: id ASC.
async def get_alert(session, user_id: int, alert_id: int) -> Alert
    - NotFoundError("Alert not found").
async def update_alert(session, user_id: int, alert_id: int, data: AlertUpdate) -> Alert
    - Aplica solo campos presentes (`data.model_dump(exclude_unset=True)`).
async def delete_alert(session, user_id: int, alert_id: int) -> None
    - Hard delete; las notifications asociadas se borran por ON DELETE CASCADE.
"""
