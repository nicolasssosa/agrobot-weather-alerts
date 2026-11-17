"""Horario de silencio por usuario (S13, D15). Función pura: sin DB.

La ventana es **semiabierta** `[start, end)` en la hora local de `users.timezone`: a las
`end` ya se envía. Si `start > end` cruza la medianoche (22:00-07:00). Ver
docs/JOBS.md §Envío agrupado por usuario y horario de silencio.
"""

import logging
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


def quiet_until(
    now: datetime, *, timezone: str, start: time | None, end: time | None
) -> datetime | None:
    """Si `now` cae en el horario de silencio, la hora UTC en que termina; si no, `None`.

    - `start is None or end is None` (o `start == end`, la DB lo prohíbe) -> `None`: sin
      silencio.
    - `now` se convierte a `timezone` y se compara contra la ventana local `[start, end)`.
    - El fin se arma con `datetime.combine(fecha_local, end, tzinfo=ZoneInfo(timezone))`
      (`fold=0`) y se pasa a UTC. Si el fin calculado quedó `<= now` (borde de un salto de
      DST) -> `None`.
    - `now` sin zona -> `ValueError`; una zona que `zoneinfo` no reconoce -> deja subir
      `ZoneInfoNotFoundError` (el dispatcher la captura y loguea).
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if start is None or end is None:
        return None
    # Ventana vacía (la DB lo impide con `quiet_hours_not_empty`): nunca hay silencio.
    if start == end:
        return None

    zone = ZoneInfo(timezone)
    local = now.astimezone(zone)
    moment = local.time()
    if start < end:
        if not start <= moment < end:
            return None
        end_date = local.date()
    elif moment >= start:
        # Después del inicio: el silencio termina mañana.
        end_date = local.date() + timedelta(days=1)
    elif moment < end:
        # Pasada la medianoche: el silencio termina hoy.
        end_date = local.date()
    else:
        return None

    until = datetime.combine(end_date, end, tzinfo=zone).astimezone(UTC)
    if until <= now:
        return None
    return until
