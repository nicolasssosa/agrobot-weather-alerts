"""Worker de alertas: `python -m app.jobs.worker [--once]`.

Un proceso corre en loop **evaluar → despachar → dormir** (docs/JOBS.md §Worker). Cada ciclo
evalúa los pronósticos contra las alertas activas en su propia transacción y después drena la
cola de notificaciones pendientes en lotes; cada lote usa un `now` fresco, así el lease del
dispatcher nunca nace vencido (S6 SEC-3). Los errores de un ciclo no matan al worker y
SIGINT/SIGTERM cortan la espera al instante, dejando terminar el ciclo en curso (RNF-04).
"""

import argparse
import asyncio
import contextlib
import logging
import signal
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.db import get_engine, get_sessionmaker
from app.core.logging import configure_logging
from app.jobs.dispatcher import DispatchResult, dispatch_pending
from app.jobs.evaluator import evaluate_alerts
from app.notifiers.base import Notifier
from app.notifiers.log import LoggingNotifier

logger = logging.getLogger(__name__)

#: Tope de lotes de despacho por ciclo. Protege contra un drenado que no progresa (un lote
#: lleno que no cambia el estado de ninguna fila). El shutdown no lo espera: lo corta
#: `stop_event` (el tope solo evita un ciclo que gire en vacío para siempre).
MAX_DISPATCH_BATCHES_PER_CYCLE = 100

#: Señales de shutdown ordenado: setean el `stop_event` y el ciclo en curso termina.
_STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)


def _utc_now() -> datetime:
    """Reloj real por defecto; inyectable en tests (nunca se usa el reloj real en ellos)."""
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class CycleResult:
    """Resultado de un ciclo: notificaciones creadas y totales del drenado."""

    created: int
    dispatch: DispatchResult


async def run_cycle(
    sessionmaker: async_sessionmaker[AsyncSession],
    notifier: Notifier,
    settings: Settings,
    *,
    now: datetime | None = None,
    clock: Callable[[], datetime] = _utc_now,
    stop_event: asyncio.Event | None = None,
) -> CycleResult:
    """Corre un ciclo completo: evalúa las alertas y drena la cola vencida.

    1. `now` (o `clock()`) da el día a evaluar y el corte de frescura de los pronósticos; la
       evaluación corre en su propia sesión y commitea, porque el dispatcher maneja sus propias
       transacciones.
    2. Se despacha en lotes mientras el lote vuelva lleno. Cada llamada obtiene un `now`
       **fresco** de `clock()`: el lease se calcula desde el momento real del claim.
    3. Si `stop_event` está seteado no se arranca un lote nuevo (S7 SEC-2): el shutdown no
       espera a drenar toda la cola, lo pendiente queda para el próximo arranque.
    4. Un lote que no cambia el estado de ninguna fila
       (`sent + retried + failed + cancelled == 0`) corta el drenado, para no girar en vacío
       si todas las filas fueron ignoradas por las guardas del dispatcher. Las canceladas
       cuentan como procesadas: un lote lleno de cancelaciones no corta el drenado porque
       puede haber más pendientes detrás. Además hay un tope de
       `MAX_DISPATCH_BATCHES_PER_CYCLE` lotes por ciclo.

    Devuelve lo creado y la suma de los lotes; loguea `cycle_done` (RNF-06).
    """
    started = time.perf_counter()
    cycle_now = now if now is not None else clock()

    async with sessionmaker() as session:
        created = await evaluate_alerts(
            session,
            today=cycle_now.date(),
            now=cycle_now,
            max_forecast_age_hours=settings.max_forecast_age_hours,
            forecast_future_tolerance_minutes=settings.forecast_future_tolerance_minutes,
        )
        await session.commit()

    sent = retried = failed = cancelled = 0
    batches = 0
    while batches < MAX_DISPATCH_BATCHES_PER_CYCLE:
        if stop_event is not None and stop_event.is_set():
            break
        batch = await dispatch_pending(
            sessionmaker,
            notifier,
            now=clock(),
            clock=clock,
            batch_size=settings.dispatch_batch_size,
            max_attempts=settings.dispatch_max_attempts,
            backoff_base_seconds=settings.dispatch_backoff_base_seconds,
            timeout_seconds=settings.notifier_timeout_seconds,
            concurrency=settings.dispatch_concurrency,
            lease_seconds=settings.dispatch_lease_seconds,
            max_forecast_age_hours=settings.max_forecast_age_hours,
        )
        batches += 1
        sent += batch.sent
        retried += batch.retried
        failed += batch.failed
        cancelled += batch.cancelled
        processed = batch.sent + batch.retried + batch.failed + batch.cancelled
        # `processed == 0`: el lote (aunque haya venido lleno) no cambió ninguna fila.
        # `processed < batch_size`: el lote no vino lleno, la cola vencida se vació.
        if processed == 0 or processed < settings.dispatch_batch_size:
            break
    else:
        logger.warning("dispatch_drain_capped batches=%d", MAX_DISPATCH_BATCHES_PER_CYCLE)

    dispatch = DispatchResult(sent=sent, retried=retried, failed=failed, cancelled=cancelled)
    duration_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "cycle_done created=%d sent=%d retried=%d failed=%d cancelled=%d duration_ms=%d",
        created,
        dispatch.sent,
        dispatch.retried,
        dispatch.failed,
        dispatch.cancelled,
        duration_ms,
    )
    return CycleResult(created=created, dispatch=dispatch)


async def run_forever(
    sessionmaker: async_sessionmaker[AsyncSession],
    notifier: Notifier,
    settings: Settings,
    stop_event: asyncio.Event,
) -> None:
    """Corre ciclos hasta que `stop_event` se setee (SIGINT/SIGTERM).

    Un error cualquiera de un ciclo se loguea y el loop sigue: un fallo transitorio de la base
    no debe matar al worker (RNF-04). `CancelledError` se propaga (cancelación del proceso).
    La espera entre ciclos es interrumpible: setear el evento la corta al instante.
    """
    while not stop_event.is_set():
        try:
            await run_cycle(sessionmaker, notifier, settings, stop_event=stop_event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("cycle_failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=settings.worker_interval_seconds)


def _install_stop_handlers(
    stop_event: asyncio.Event, loop: asyncio.AbstractEventLoop | None = None
) -> None:
    """Registra SIGINT/SIGTERM para setear `stop_event` (graceful shutdown)."""
    running_loop = loop if loop is not None else asyncio.get_running_loop()
    for sig in _STOP_SIGNALS:
        running_loop.add_signal_handler(sig, stop_event.set)


def _remove_stop_handlers(loop: asyncio.AbstractEventLoop) -> None:
    """Desregistra las señales al salir (el loop de `asyncio.run` se cierra)."""
    for sig in _STOP_SIGNALS:
        loop.remove_signal_handler(sig)


async def _run(settings: Settings, notifier: Notifier, *, once: bool) -> None:
    """Arranca el worker y garantiza el `dispose()` del engine al salir."""
    sessionmaker = get_sessionmaker()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    _install_stop_handlers(stop_event, loop)
    try:
        if once:
            await run_cycle(sessionmaker, notifier, settings)
        else:
            await run_forever(sessionmaker, notifier, settings, stop_event)
    finally:
        _remove_stop_handlers(loop)
        await get_engine().dispose()
        # `lru_cache`: sin limpiar, un segundo `main()` en el mismo proceso reusaría un engine
        # disposed (su pool quedó atado a un event loop ya cerrado), igual que en ingest.py.
        get_engine.cache_clear()
        get_sessionmaker.cache_clear()


def main(argv: Sequence[str] | None = None) -> None:
    """Punto de entrada: loop del worker o un solo ciclo con `--once`."""
    configure_logging()
    parser = argparse.ArgumentParser(description="Worker de alertas climáticas")
    parser.add_argument(
        "--once",
        action="store_true",
        help="corre un solo ciclo (evaluar + despachar) y sale",
    )
    args = parser.parse_args(argv)
    asyncio.run(_run(get_settings(), LoggingNotifier(), once=args.once))


if __name__ == "__main__":
    main()
