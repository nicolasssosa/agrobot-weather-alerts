"""Evaluación de alertas vs. pronósticos almacenados. TODO(S5).

Spec completa: docs/JOBS.md §Evaluador.

async def evaluate_alerts(session: AsyncSession, *, today: date) -> int
    - UN solo `INSERT ... SELECT ... ON CONFLICT (alert_id, forecast_date) DO NOTHING
      RETURNING id` (set-based, idempotente, seguro con N workers concurrentes).
    - Devuelve la cantidad de notificaciones NUEVAS creadas.
    - NO commitea (lo hace el llamador).
"""
