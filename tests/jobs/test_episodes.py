"""Tests de la regla de transición de episodios (docs/JOBS.md §Episodios, S12b/D1).

Son tests puros de `app/jobs/episodes.py`: cada fila de la tabla de transición y los bordes de
`rearm_level` (incluido el mínimo 1). La equivalencia con el SQL del evaluador está en
`tests/jobs/test_evaluator.py`.
"""

import pytest

from app.jobs.episodes import INITIAL_STATE, DayState, rearm_level, reset, step

#: Umbral y margen de referencia: dispara en >= 70, re-arma en < 60.
THRESHOLD = 70
MARGIN = 10


def test_initial_state_is_armed_with_no_episode() -> None:
    """Sin fila en `alert_day_states`: armada y episodio 0 (el primer cruce avisa el 1)."""
    assert DayState(episode=0, armed=True) == INITIAL_STATE


# --- Tabla de transición ----------------------------------------------------


def test_armed_day_reaching_the_threshold_fires_the_next_episode() -> None:
    new_state, notifies = step(
        INITIAL_STATE, probability=THRESHOLD, threshold=THRESHOLD, rearm_margin=MARGIN
    )

    assert (new_state, notifies) == (DayState(episode=1, armed=False), True)


def test_armed_day_above_the_threshold_fires_the_next_episode() -> None:
    new_state, notifies = step(
        DayState(episode=2, armed=True), probability=95, threshold=THRESHOLD, rearm_margin=MARGIN
    )

    assert (new_state, notifies) == (DayState(episode=3, armed=False), True)


def test_armed_day_below_the_threshold_does_not_fire() -> None:
    new_state, notifies = step(
        DayState(episode=2, armed=True),
        probability=THRESHOLD - 1,
        threshold=THRESHOLD,
        rearm_margin=MARGIN,
    )

    assert (new_state, notifies) == (DayState(episode=2, armed=True), False)


def test_fired_day_below_the_rearm_level_arms_without_notifying() -> None:
    new_state, notifies = step(
        DayState(episode=2, armed=False),
        probability=rearm_level(THRESHOLD, MARGIN) - 1,  # 59
        threshold=THRESHOLD,
        rearm_margin=MARGIN,
    )

    assert (new_state, notifies) == (DayState(episode=2, armed=True), False)


@pytest.mark.parametrize("probability", [60, 69, 70, 95, 100])
def test_fired_day_at_or_above_the_rearm_level_does_not_change(probability: int) -> None:
    """Banda de histéresis y "sigue arriba": no re-arma ni re-notifica."""
    state = DayState(episode=2, armed=False)

    new_state, notifies = step(
        state, probability=probability, threshold=THRESHOLD, rearm_margin=MARGIN
    )

    assert (new_state, notifies) == (state, False)


def test_the_hysteresis_band_does_not_renotify_while_oscillating() -> None:
    """Baja a 61 y vuelve a 90: no re-arma, así que no hay episodio nuevo."""
    state = DayState(episode=1, armed=False)

    state, notifies_down = step(state, probability=61, threshold=THRESHOLD, rearm_margin=MARGIN)
    state, notifies_up = step(state, probability=90, threshold=THRESHOLD, rearm_margin=MARGIN)

    assert (state, notifies_down, notifies_up) == (DayState(episode=1, armed=False), False, False)


def test_crossing_down_and_back_up_fires_a_new_episode() -> None:
    state = DayState(episode=1, armed=False)

    state, notifies_down = step(state, probability=59, threshold=THRESHOLD, rearm_margin=MARGIN)
    state, notifies_up = step(state, probability=70, threshold=THRESHOLD, rearm_margin=MARGIN)

    assert (state, notifies_down, notifies_up) == (DayState(episode=2, armed=False), False, True)


def test_a_fired_day_with_a_zero_margin_rearms_just_below_the_threshold() -> None:
    """Con margen 0 no hay banda: `threshold - 1` re-arma (el borde es `< rearm_level`)."""
    assert rearm_level(THRESHOLD, 0) == THRESHOLD

    new_state, notifies = step(
        DayState(episode=1, armed=False),
        probability=THRESHOLD - 1,
        threshold=THRESHOLD,
        rearm_margin=0,
    )

    assert (new_state, notifies) == (DayState(episode=1, armed=True), False)


# --- `rearm_level`: bordes --------------------------------------------------


@pytest.mark.parametrize(
    ("threshold", "margin", "expected"),
    [
        (70, 10, 60),  # caso documentado
        (70, 0, 70),  # sin margen: re-arma en 69
        (100, 99, 1),  # 1 - 1 = 0 -> mínimo 1
        (100, 10, 90),
        (5, 10, 1),  # el mínimo siempre es 1
        (1, 0, 1),
        (1, 99, 1),
        (2, 99, 1),
        (51, 50, 1),
        (52, 50, 2),
    ],
)
def test_rearm_level_never_goes_below_one(threshold: int, margin: int, expected: int) -> None:
    assert rearm_level(threshold, margin) == expected


def test_with_threshold_one_a_fired_day_only_rearms_at_zero() -> None:
    """`rearm_level(1, 10) == 1`: con probabilidad 0 se re-arma; con 1 no."""
    state = DayState(episode=1, armed=False)

    state, notifies_zero = step(state, probability=0, threshold=1, rearm_margin=10)
    state, notifies_one = step(state, probability=1, threshold=1, rearm_margin=10)

    assert (state, notifies_zero, notifies_one) == (DayState(episode=2, armed=False), False, True)


# --- `reset` ----------------------------------------------------------------


def test_reset_arms_and_keeps_the_episode() -> None:
    """Cambio de umbral o reactivación: el próximo cruce avisa el episodio siguiente."""
    assert reset(DayState(episode=3, armed=False)) == DayState(episode=3, armed=True)


def test_reset_of_an_already_armed_day_is_a_noop() -> None:
    state = DayState(episode=2, armed=True)

    assert reset(state) == state


def test_reset_of_the_initial_state_is_the_initial_state() -> None:
    assert reset(INITIAL_STATE) == INITIAL_STATE
