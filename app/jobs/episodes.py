"""Regla de transición de episodios por (alerta, día), en Python puro.

Spec: docs/JOBS.md §Episodios (D1). El evaluador aplica la misma regla en SQL
(`app/jobs/evaluator.py`); estas funciones son la referencia de los tests: cualquier
divergencia entre ambas aparecería en el test de equivalencia.

Sin fila en `alert_day_states` el día está **armado** con episodio 0 (`INITIAL_STATE`):
el primer cruce hacia arriba notifica el episodio 1.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DayState:
    """Estado de un (alerta, día): último episodio disparado y si espera un cruce."""

    episode: int
    #: `True` = el próximo `probability >= threshold` dispara `episode + 1`.
    armed: bool


#: Estado de un día que nunca disparó (sin fila en `alert_day_states`).
INITIAL_STATE = DayState(episode=0, armed=True)


def rearm_level(threshold: int, rearm_margin: int) -> int:
    """Probabilidad por debajo de la cual un día disparado se vuelve a armar.

    `max(threshold - rearm_margin, 1)`: 1 es el mínimo del rango de probabilidad, así que
    un umbral 1 con cualquier margen sigue teniendo banda de histéresis vacía (re-arma en 0).
    """
    return max(threshold - rearm_margin, 1)


def step(
    state: DayState, *, probability: int, threshold: int, rearm_margin: int
) -> tuple[DayState, bool]:
    """Aplica un ciclo de evaluación: `(nuevo estado, ¿notifica?)`.

    - Armado y `probability >= threshold`: dispara el episodio `episode + 1` (queda
      desarmado hasta que la probabilidad baje de `rearm_level`).
    - Disparado y `probability < rearm_level`: se re-arma sin notificar.
    - Cualquier otro caso (banda de histéresis incluida): sin cambios.
    """
    if state.armed and probability >= threshold:
        return DayState(episode=state.episode + 1, armed=False), True
    if not state.armed and probability < rearm_level(threshold, rearm_margin):
        return DayState(episode=state.episode, armed=True), False
    return state, False


def reset(state: DayState) -> DayState:
    """Re-arma por cambio de umbral o reactivación de la alerta, conservando el episodio."""
    return DayState(episode=state.episode, armed=True)
