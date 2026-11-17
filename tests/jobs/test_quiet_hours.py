"""Tests de `quiet_until` (docs/TESTING.md §test_quiet_hours, docs/JOBS.md §S13).

Todos los instantes son UTC fijos (nunca el reloj real) y la zona usada (`America/Argentina/
Buenos_Aires`, UTC-3 sin DST en la tz database actual) permite calcular el esperado a mano.
"""

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfoNotFoundError

import pytest

from app.jobs.quiet_hours import quiet_until

TZ = "America/Argentina/Buenos_Aires"


def _utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# --- Sin configuración ------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end"),
    [(None, None), (None, time(22, 0)), (time(22, 0), None)],
)
def test_without_a_configured_window_there_is_no_silence(
    start: time | None, end: time | None
) -> None:
    assert quiet_until(_utc(2026, 11, 16, 3), timezone=TZ, start=start, end=end) is None


def test_a_window_with_equal_boundaries_is_never_quiet() -> None:
    """La DB lo prohíbe (`quiet_hours_not_empty`); la función pura no difiere nada."""
    assert (
        quiet_until(_utc(2026, 11, 16, 3), timezone=TZ, start=time(22, 0), end=time(22, 0)) is None
    )


# --- Ventana del mismo día (13:00-15:00 local = 16:00-18:00 UTC) ------------


@pytest.mark.parametrize(
    ("utc_moment", "expected"),
    [
        # Adentro: 14:30 local -> termina hoy a las 15:00 (= 18:00 UTC).
        (_utc(2026, 11, 16, 17, 30), _utc(2026, 11, 16, 18, 0)),
        # Borde `start` incluido: 13:00 local.
        (_utc(2026, 11, 16, 16, 0), _utc(2026, 11, 16, 18, 0)),
        # Borde `end` excluido: 15:00 local ya se envía.
        (_utc(2026, 11, 16, 18, 0), None),
        # Antes de la ventana: 12:59 local.
        (_utc(2026, 11, 16, 15, 59), None),
        # Después de la ventana: 15:01 local.
        (_utc(2026, 11, 16, 18, 1), None),
        # Muy temprano (03:00 local).
        (_utc(2026, 11, 16, 6, 0), None),
    ],
)
def test_a_same_day_window_uses_the_half_open_local_interval(
    utc_moment: datetime, expected: datetime | None
) -> None:
    assert quiet_until(utc_moment, timezone=TZ, start=time(13, 0), end=time(15, 0)) == expected


# --- Ventana que cruza la medianoche (22:00-07:00 local) --------------------


@pytest.mark.parametrize(
    ("utc_moment", "expected"),
    [
        # Antes de la medianoche local: 23:30 -> termina mañana a las 07:00 local.
        (_utc(2026, 11, 17, 2, 30), _utc(2026, 11, 17, 10, 0)),
        # Borde `start` incluido: 22:00 local -> mañana 07:00 local.
        (_utc(2026, 11, 17, 1, 0), _utc(2026, 11, 17, 10, 0)),
        # Después de la medianoche local: 01:30 del día siguiente -> hoy 07:00 local.
        (_utc(2026, 11, 17, 4, 30), _utc(2026, 11, 17, 10, 0)),
        # 06:59 local: adentro, termina hoy.
        (_utc(2026, 11, 17, 9, 59), _utc(2026, 11, 17, 10, 0)),
        # Borde `end` excluido: 07:00 local ya se envía.
        (_utc(2026, 11, 17, 10, 0), None),
        # Fuera de la ventana: 12:00 local.
        (_utc(2026, 11, 17, 15, 0), None),
        # Justo antes del inicio: 21:59 local.
        (_utc(2026, 11, 17, 0, 59), None),
    ],
)
def test_a_window_crossing_midnight_starts_today_and_ends_tomorrow(
    utc_moment: datetime, expected: datetime | None
) -> None:
    assert quiet_until(utc_moment, timezone=TZ, start=time(22, 0), end=time(7, 0)) == expected


# --- Conversión desde UTC a una zona no UTC ---------------------------------


def test_the_utc_instant_is_converted_to_the_user_timezone_before_comparing() -> None:
    """01:30 UTC es 22:30 local del día anterior (UTC-3): la ventana ya empezó."""
    until = quiet_until(_utc(2026, 11, 17, 1, 30), timezone=TZ, start=time(22, 0), end=time(7, 0))

    # 07:00 local del 17/11 = 10:00 UTC.
    assert until == _utc(2026, 11, 17, 10, 0)
    assert until is not None
    assert until.tzinfo is not None
    assert until.tzinfo.utcoffset(until).total_seconds() == 0


def test_a_utc_user_timezone_returns_the_same_clock_time() -> None:
    until = quiet_until(_utc(2026, 11, 16, 14, 0), timezone="UTC", start=time(13), end=time(15))

    assert until == _utc(2026, 11, 16, 15, 0)


# --- Validaciones -----------------------------------------------------------


def test_a_naive_now_raises_value_error() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        quiet_until(
            datetime(2026, 11, 16, 14, 0),
            timezone=TZ,
            start=time(13, 0),
            end=time(15, 0),
        )


def test_an_unknown_timezone_raises_zoneinfo_not_found() -> None:
    """El dispatcher captura el error y loguea `invalid_user_timezone` (JOBS.md §S13)."""
    with pytest.raises(ZoneInfoNotFoundError):
        quiet_until(
            _utc(2026, 11, 16, 14, 0),
            timezone="Mars/Olympus",
            start=time(13, 0),
            end=time(15, 0),
        )


# --- Ventana de un día completo (00:00-23:59) -------------------------------


@pytest.mark.parametrize(
    ("utc_moment", "expected"),
    [
        # 00:00 local del 16/11: primer instante de la ventana (borde `start` incluido).
        (_utc(2026, 11, 16, 3, 0), _utc(2026, 11, 17, 2, 59)),
        # 12:00 local: adentro.
        (_utc(2026, 11, 16, 15, 0), _utc(2026, 11, 17, 2, 59)),
        # 23:58 local: adentro, a un minuto del fin.
        (_utc(2026, 11, 17, 2, 58), _utc(2026, 11, 17, 2, 59)),
        # 23:59 local: borde `end` excluido, ya se envía.
        (_utc(2026, 11, 17, 2, 59), None),
        # 00:00 local del día siguiente: vuelve a empezar la ventana.
        (_utc(2026, 11, 17, 3, 0), _utc(2026, 11, 18, 2, 59)),
    ],
)
def test_a_full_day_window_is_half_open_and_ends_right_before_midnight(
    utc_moment: datetime, expected: datetime | None
) -> None:
    assert quiet_until(utc_moment, timezone=TZ, start=time(0, 0), end=time(23, 59)) == expected


# --- Cambios de horario de verano (DST) -------------------------------------
#
# JOBS.md §S13: el fin se arma con `datetime.combine(fecha, end, tzinfo=zone)` (`fold=0`):
# `zoneinfo` resuelve con el offset previo una hora local inexistente, y si el fin queda
# `<= now` no hay silencio (no se difiere de más en el día del cambio).


def test_the_end_inside_a_dst_gap_resolves_with_the_pre_spring_offset() -> None:
    """America/Santiago salta de 00:00 a 01:00 el 2026-09-06: las 00:30 no existen.

    Una ventana 23:00-00:30 abierta la noche anterior termina en esa hora inexistente:
    con `fold=0` el fin cae a las 04:30 UTC (01:30 local, ya con el offset de verano, -03)
    y sigue siendo posterior a `now`.
    """
    until = quiet_until(
        _utc(2026, 9, 6, 3, 30),  # 2026-09-05 23:30 local (-04)
        timezone="America/Santiago",
        start=time(23, 0),
        end=time(0, 30),
    )

    assert until == _utc(2026, 9, 6, 4, 30)


def test_an_ambiguous_end_already_passed_in_a_dst_fold_is_not_quiet() -> None:
    """America/New_York repite 01:00-02:00 el 2026-11-01: 01:15 ocurre dos veces.

    A la segunda (01:15 EST = 06:15 UTC) el fin con `fold=0` (01:30 EDT = 05:30 UTC) ya
    pasó: el fin calculado es `<= now`, así que no se difiere 24 h por el salto de DST.
    """
    until = quiet_until(
        _utc(2026, 11, 1, 6, 15),
        timezone="America/New_York",
        start=time(0, 30),
        end=time(1, 30),
    )

    assert until is None
