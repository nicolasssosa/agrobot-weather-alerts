"""Despacho de notificaciones pendientes (patrón outbox). TODO(S6).

Spec completa: docs/JOBS.md §Dispatcher.

@dataclass(frozen=True, slots=True)
class DispatchResult:
    sent: int
    retried: int
    failed: int

def build_message_text(*, event_type: EventType, field_name: str, probability: int,
                       threshold: int, forecast_date: date) -> str

async def dispatch_pending(
    session: AsyncSession, notifier: Notifier, *, now: datetime, batch_size: int,
    max_attempts: int, backoff_base_seconds: int, timeout_seconds: float,
) -> DispatchResult
    - SELECT ... FOR UPDATE OF notifications SKIP LOCKED, procesa el lote, NO commitea.
"""
