"""Tests del worker (docs/TESTING.md §`tests/jobs/test_worker.py`, docs/JOBS.md §Worker).

El reloj se inyecta siempre: `now` fija el día evaluado y `clock` la hora de cada claim. Como
el evaluador crea las notificaciones con `now()` de Postgres (hora real del test), el reloj de
claim parte de una fecha fija muy posterior: así nunca depende del reloj de pared.
"""

import asyncio
import contextlib
import itertools
import logging
import signal
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.jobs.worker as worker
from app.core.config import Settings
from app.jobs.dispatcher import DispatchResult, dispatch_pending
from app.jobs.worker import (
    MAX_DISPATCH_BATCHES_PER_CYCLE,
    CycleResult,
    _install_stop_handlers,
    run_cycle,
    run_forever,
)
from app.models import EventType, Notification, NotificationStatus
from app.notifiers.base import NotificationMessage
from tests.conftest import (
    FakeNotifier,
    MakeAlert,
    MakeField,
    MakeForecast,
    MakeUser,
)

#: Día evaluado (fijo). Lejano al reloj real para que las notificaciones del evaluador
#: (`next_attempt_at = now()` de Postgres) ya estén vencidas al reclamar.
EVAL_NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
#: Hora de claim (fija y posterior a cualquier `now()` real del test, y dentro de la ventana
#: de frescura del pronóstico emitido en `EVAL_NOW`).
CLAIM_NOW = datetime(2030, 1, 1, 12, 1, tzinfo=UTC)
FORECAST_DATE = date(2030, 1, 3)


def _settings(*, batch_size: int = 10, interval: int = 60) -> Settings:
    """Settings deterministas para el worker (el lease cubre el peor caso del lote)."""
    return Settings(
        worker_interval_seconds=interval,
        dispatch_batch_size=batch_size,
        dispatch_max_attempts=3,
        dispatch_backoff_base_seconds=30,
        notifier_timeout_seconds=1.0,
        dispatch_concurrency=10,
        dispatch_lease_seconds=60,
    )


def _clock_at(moment: datetime) -> Callable[[], datetime]:
    """Reloj fijo."""
    return lambda: moment


class _AdvancingClock:
    """Reloj inyectable: avanza `step` en cada llamada (nunca el reloj real)."""

    def __init__(self, start: datetime, step: timedelta) -> None:
        self._current = start
        self._step = step

    def __call__(self) -> datetime:
        moment = self._current
        self._current += self._step
        return moment


# --- run_cycle end-to-end ---------------------------------------------------


async def test_run_cycle_evaluates_and_sends_end_to_end(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_field: MakeField,
    make_forecast: MakeForecast,
    make_alert: MakeAlert,
    fake_notifier: FakeNotifier,
) -> None:
    """Evaluar + despachar: la notificación disparada se envía y queda `sent`."""
    owner = await make_user(phone_number="+5491100000001", name="Ana Gómez")
    field = await make_field(owner, name="Lote Norte")
    await make_alert(field, event_type=EventType.FROST, threshold=70)
    await make_forecast(
        field,
        forecast_date=FORECAST_DATE,
        event_type=EventType.FROST,
        probability=85,
        issued_at=EVAL_NOW,
    )
    # Bajo el umbral: no debe generar notificación.
    await make_forecast(
        field,
        forecast_date=FORECAST_DATE + timedelta(days=1),
        event_type=EventType.FROST,
        probability=10,
        issued_at=EVAL_NOW,
    )

    result = await run_cycle(
        test_sessionmaker,
        fake_notifier,
        _settings(),
        now=EVAL_NOW,
        clock=_clock_at(CLAIM_NOW),
    )

    assert result == CycleResult(
        created=1, dispatch=DispatchResult(sent=1, retried=0, failed=0, users=1)
    )
    notification = await db_session.scalar(select(Notification))
    assert notification is not None
    assert fake_notifier.sent == [
        NotificationMessage(
            notification_id=notification.id,
            phone_number="+5491100000001",
            text=(
                '⚠️ Alerta de helada en "Lote Norte": 85% de probabilidad '
                "para el 03/01/2030 (tu umbral: 70%)."
            ),
        )
    ]
    await db_session.refresh(notification)
    assert notification.status == NotificationStatus.SENT
    assert notification.sent_at == CLAIM_NOW
    assert notification.attempts == 1

    # Segunda corrida: no crea ni reenvía nada (idempotencia).
    sent_before = list(fake_notifier.sent)
    second = await run_cycle(
        test_sessionmaker,
        fake_notifier,
        _settings(),
        now=EVAL_NOW,
        clock=_clock_at(CLAIM_NOW),
    )

    assert second == CycleResult(created=0, dispatch=DispatchResult(0, 0, 0))
    assert fake_notifier.sent == sent_before
    assert (await db_session.scalar(select(Notification.id))) is not None


async def test_run_cycle_drains_several_batches_with_a_fresh_now_per_batch(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_field: MakeField,
    make_forecast: MakeForecast,
    make_alert: MakeAlert,
    fake_notifier: FakeNotifier,
    monkeypatch,
) -> None:
    """Con `batch_size=2` (usuarios) y 5 usuarios con avisos se hacen 3 lotes."""
    for _ in range(5):
        field = await make_field(await make_user())
        await make_alert(field, event_type=EventType.FROST, threshold=70)
        await make_forecast(
            field,
            forecast_date=FORECAST_DATE,
            event_type=EventType.FROST,
            probability=90,
            issued_at=EVAL_NOW,
        )

    recorded: list[datetime] = []
    real_dispatch = dispatch_pending

    async def recording_dispatch(sessionmaker, notifier, **kwargs):  # type: ignore[no-untyped-def]
        recorded.append(kwargs["now"])
        return await real_dispatch(sessionmaker, notifier, **kwargs)

    monkeypatch.setattr(worker, "dispatch_pending", recording_dispatch)
    clock = _AdvancingClock(CLAIM_NOW, timedelta(minutes=1))

    result = await run_cycle(
        test_sessionmaker,
        fake_notifier,
        _settings(batch_size=2),
        now=EVAL_NOW,
        clock=clock,
    )

    assert result.created == 5
    assert result.dispatch == DispatchResult(sent=5, retried=0, failed=0, users=5)
    assert len(fake_notifier.sent) == 5
    assert len(recorded) == 3
    # Un `now` fresco y distinto por lote (SEC-3), en orden creciente.
    assert recorded == sorted(recorded)
    assert len(set(recorded)) == 3


# --- Protección anti-loop del drenado --------------------------------------


async def test_run_cycle_does_not_start_a_new_batch_after_stop_event(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_field: MakeField,
    make_forecast: MakeForecast,
    make_alert: MakeAlert,
) -> None:
    """Si el notifier setea `stop_event` en el primer lote, no se arranca el segundo (SEC-2)."""
    for _ in range(4):
        field = await make_field(await make_user())
        await make_alert(field, event_type=EventType.FROST, threshold=70)
        await make_forecast(
            field,
            forecast_date=FORECAST_DATE,
            event_type=EventType.FROST,
            probability=90,
            issued_at=EVAL_NOW,
        )

    stop_event = asyncio.Event()

    class _StoppingNotifier:
        """Notifier que registra el envío y pide el shutdown del worker."""

        def __init__(self) -> None:
            self.sent: list[NotificationMessage] = []

        async def send(self, message: NotificationMessage) -> None:
            self.sent.append(message)
            stop_event.set()

    notifier = _StoppingNotifier()

    result = await run_cycle(
        test_sessionmaker,
        notifier,
        _settings(batch_size=2),
        now=EVAL_NOW,
        clock=_clock_at(CLAIM_NOW),
        stop_event=stop_event,
    )

    # Solo se envió el primer lote (2 usuarios); las 2 pendientes quedan para el próximo arranque.
    assert result.created == 4
    assert result.dispatch == DispatchResult(sent=2, retried=0, failed=0, users=2)
    assert len(notifier.sent) == 2
    statuses = (await db_session.scalars(select(Notification.status))).all()
    assert statuses.count(NotificationStatus.SENT) == 2
    assert statuses.count(NotificationStatus.PENDING) == 2


async def test_run_cycle_skips_all_batches_when_the_event_is_set_during_evaluation(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    fake_notifier: FakeNotifier,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    """S7 SEC-3: un SIGTERM durante la evaluación no arranca ni el primer lote."""
    stop_event = asyncio.Event()
    dispatch_calls: list[dict[str, object]] = []

    async def evaluate_and_stop(session, **kwargs):  # type: ignore[no-untyped-def]
        stop_event.set()
        return 0

    async def dispatch_that_must_not_run(sessionmaker, notifier, **kwargs):  # type: ignore[no-untyped-def]
        dispatch_calls.append(kwargs)
        return DispatchResult(sent=0, retried=0, failed=0)

    monkeypatch.setattr(worker, "evaluate_alerts", evaluate_and_stop)
    monkeypatch.setattr(worker, "dispatch_pending", dispatch_that_must_not_run)

    result = await run_cycle(
        test_sessionmaker,
        fake_notifier,
        _settings(),
        now=EVAL_NOW,
        clock=_clock_at(CLAIM_NOW),
        stop_event=stop_event,
    )

    assert result.dispatch == DispatchResult(sent=0, retried=0, failed=0)
    assert dispatch_calls == []


async def test_run_cycle_stops_draining_when_a_batch_changes_nothing(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    fake_notifier: FakeNotifier,
    monkeypatch,
) -> None:
    """Un lote lleno que no cambió ninguna fila (todas ignoradas por guardas) corta el drenado."""
    calls = 0

    async def zero_batch(sessionmaker, notifier, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return DispatchResult(sent=0, retried=0, failed=0)

    monkeypatch.setattr(worker, "dispatch_pending", zero_batch)

    result = await run_cycle(
        test_sessionmaker,
        fake_notifier,
        _settings(batch_size=2),
        now=EVAL_NOW,
        clock=_clock_at(CLAIM_NOW),
    )

    assert result == CycleResult(created=0, dispatch=DispatchResult(0, 0, 0))
    assert calls == 1


async def test_run_cycle_accumulates_cancelled_and_keeps_draining_a_full_batch(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    fake_notifier: FakeNotifier,
    monkeypatch,
    caplog,
) -> None:
    """S9/S13: `cancelled` se acumula y un lote lleno de canceladas no corta el drenado.

    Lo que decide seguir es `users == batch_size` (usuarios procesados), no la cantidad de
    notificaciones enviadas.
    """
    calls = 0

    async def cancelling_then_sending(sessionmaker, notifier, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            return DispatchResult(
                sent=0,
                retried=0,
                failed=0,
                cancelled=kwargs["batch_size"],
                users=kwargs["batch_size"],
            )
        return DispatchResult(sent=1, retried=0, failed=0, users=1)

    monkeypatch.setattr(worker, "dispatch_pending", cancelling_then_sending)

    with caplog.at_level(logging.INFO, logger="app.jobs.worker"):
        result = await run_cycle(
            test_sessionmaker,
            fake_notifier,
            _settings(batch_size=2),
            now=EVAL_NOW,
            clock=_clock_at(CLAIM_NOW),
        )

    assert calls == 2
    assert result.dispatch == DispatchResult(sent=1, retried=0, failed=0, cancelled=2, users=3)
    assert any(
        "cycle_done" in record.getMessage() and "cancelled=2" in record.getMessage()
        for record in caplog.records
    )


async def test_run_cycle_does_not_dispatch_when_evaluate_alerts_raises(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    fake_notifier: FakeNotifier,
    monkeypatch,
) -> None:
    """Si el evaluador falla, la excepción sube y no se despacha nada a medias."""
    dispatched: list[int] = []

    async def failing_evaluate(  # type: ignore[no-untyped-def]
        session,
        *,
        now,
        horizon_days,
        max_forecast_age_hours,
        future_tolerance_minutes,
        rearm_margin,
    ):
        raise RuntimeError("evaluation down")

    async def recording_dispatch(sessionmaker, notifier, **kwargs):  # type: ignore[no-untyped-def]
        dispatched.append(1)
        return DispatchResult(sent=0, retried=0, failed=0)

    monkeypatch.setattr(worker, "evaluate_alerts", failing_evaluate)
    monkeypatch.setattr(worker, "dispatch_pending", recording_dispatch)

    with pytest.raises(RuntimeError, match="evaluation down"):
        await run_cycle(
            test_sessionmaker,
            fake_notifier,
            _settings(),
            now=EVAL_NOW,
            clock=_clock_at(CLAIM_NOW),
        )

    assert dispatched == []
    assert fake_notifier.sent == []
    # La transacción de evaluación se revirtió: no quedó ninguna notificación.
    assert (await db_session.scalar(select(Notification.id))) is None


async def test_run_cycle_keeps_committed_work_when_a_later_batch_raises(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_field: MakeField,
    make_forecast: MakeForecast,
    make_alert: MakeAlert,
    fake_notifier: FakeNotifier,
    monkeypatch,
) -> None:
    """Un lote que lanza no pierde lo ya evaluado/commiteado; el ciclo siguiente lo drena."""
    for _ in range(5):
        field = await make_field(await make_user())
        await make_alert(field, event_type=EventType.FROST, threshold=70)
        await make_forecast(
            field,
            forecast_date=FORECAST_DATE,
            event_type=EventType.FROST,
            probability=90,
            issued_at=EVAL_NOW,
        )

    calls = 0
    real_dispatch = dispatch_pending

    async def flaky_dispatch(sessionmaker, notifier, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("batch down")
        return await real_dispatch(sessionmaker, notifier, **kwargs)

    monkeypatch.setattr(worker, "dispatch_pending", flaky_dispatch)

    with pytest.raises(RuntimeError, match="batch down"):
        await run_cycle(
            test_sessionmaker,
            fake_notifier,
            _settings(batch_size=2),
            now=EVAL_NOW,
            clock=_clock_at(CLAIM_NOW),
        )

    assert calls == 2
    statuses = (await db_session.scalars(select(Notification.status))).all()
    # Las 5 notificaciones del evaluador sobreviven al fallo: 2 ya enviadas, 3 pendientes.
    assert len(statuses) == 5
    assert statuses.count(NotificationStatus.SENT) == 2
    assert statuses.count(NotificationStatus.PENDING) == 3

    # Sin el error, el ciclo siguiente no re-evalúa y drena lo que quedó pendiente.
    monkeypatch.undo()
    second = await run_cycle(
        test_sessionmaker,
        fake_notifier,
        _settings(batch_size=2),
        now=EVAL_NOW,
        clock=_clock_at(CLAIM_NOW),
    )

    assert second == CycleResult(
        created=0, dispatch=DispatchResult(sent=3, retried=0, failed=0, users=3)
    )
    assert fake_notifier.sent != []


async def test_run_cycle_caps_the_number_of_batches_per_cycle(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    fake_notifier: FakeNotifier,
    monkeypatch,
) -> None:
    """Un dispatch que siempre devuelve un lote lleno termina en el tope documentado."""
    calls = 0

    async def always_full(sessionmaker, notifier, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return DispatchResult(sent=1, retried=0, failed=0, users=kwargs["batch_size"])

    monkeypatch.setattr(worker, "dispatch_pending", always_full)

    result = await run_cycle(
        test_sessionmaker,
        fake_notifier,
        _settings(batch_size=2),
        now=EVAL_NOW,
        clock=_clock_at(CLAIM_NOW),
    )

    assert calls == MAX_DISPATCH_BATCHES_PER_CYCLE
    assert result.dispatch == DispatchResult(
        sent=MAX_DISPATCH_BATCHES_PER_CYCLE,
        retried=0,
        failed=0,
        users=MAX_DISPATCH_BATCHES_PER_CYCLE * 2,
    )


# --- run_forever ------------------------------------------------------------


async def test_run_forever_stops_when_the_event_is_set(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    fake_notifier: FakeNotifier,
    monkeypatch,
) -> None:
    """Si `run_cycle` setea el evento, el loop termina sin error (graceful shutdown)."""
    stop_event = asyncio.Event()
    calls = 0

    async def cycle_then_stop(sessionmaker, notifier, settings, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        stop_event.set()
        return CycleResult(created=0, dispatch=DispatchResult(0, 0, 0))

    monkeypatch.setattr(worker, "run_cycle", cycle_then_stop)

    await asyncio.wait_for(
        run_forever(test_sessionmaker, fake_notifier, _settings(interval=3600), stop_event),
        timeout=2,
    )

    assert calls == 1


async def test_run_forever_survives_a_persistent_error_without_a_tight_loop(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    fake_notifier: FakeNotifier,
    monkeypatch,
    caplog,
) -> None:
    """Con todos los ciclos fallando sigue vivo, loguea `cycle_failed` y respeta la espera."""
    stop_event = asyncio.Event()
    starts: list[float] = []

    async def always_fails(sessionmaker, notifier, settings, **kwargs):  # type: ignore[no-untyped-def]
        starts.append(time.perf_counter())
        raise RuntimeError("db down")

    monkeypatch.setattr(worker, "run_cycle", always_fails)

    async def stop_later() -> None:
        await asyncio.sleep(2.2)
        stop_event.set()

    stopper = asyncio.create_task(stop_later())
    try:
        with caplog.at_level(logging.ERROR):
            await asyncio.wait_for(
                run_forever(
                    test_sessionmaker,
                    fake_notifier,
                    _settings(interval=1),
                    stop_event,
                ),
                timeout=10,
            )
    finally:
        stopper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stopper

    failures = [record for record in caplog.records if "cycle_failed" in record.getMessage()]
    # Intervalo de 1 s durante ~2,2 s: 2 o 3 ciclos (un tight-loop daría cientos).
    assert 2 <= len(starts) <= 4
    assert len(failures) == len(starts)
    gaps = [later - earlier for earlier, later in itertools.pairwise(starts)]
    assert gaps and all(gap >= 0.9 for gap in gaps)


async def test_stop_requested_during_a_cycle_finishes_it_and_exits(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    fake_notifier: FakeNotifier,
    monkeypatch,
) -> None:
    """SIGTERM (stop_event desde otra tarea) deja terminar el ciclo en curso y no arranca otro."""
    stop_event = asyncio.Event()
    starts: list[int] = []
    finished: list[int] = []

    async def slow_cycle(sessionmaker, notifier, settings, **kwargs):  # type: ignore[no-untyped-def]
        starts.append(1)
        await asyncio.sleep(0.2)
        finished.append(1)
        return CycleResult(created=0, dispatch=DispatchResult(0, 0, 0))

    monkeypatch.setattr(worker, "run_cycle", slow_cycle)

    async def sigterm() -> None:
        await asyncio.sleep(0.05)
        stop_event.set()

    stopper = asyncio.create_task(sigterm())
    try:
        await asyncio.wait_for(
            run_forever(
                test_sessionmaker,
                fake_notifier,
                _settings(interval=3600),
                stop_event,
            ),
            timeout=5,
        )
    finally:
        stopper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stopper

    assert starts == [1]
    assert finished == [1]
    assert stop_event.is_set()


async def test_run_forever_keeps_going_after_a_failed_cycle(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    fake_notifier: FakeNotifier,
    monkeypatch,
    caplog,
) -> None:
    """Un error transitorio no mata al worker: el ciclo siguiente corre normalmente."""
    stop_event = asyncio.Event()
    calls = 0

    async def flaky_cycle(sessionmaker, notifier, settings, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient db error")
        stop_event.set()
        return CycleResult(created=0, dispatch=DispatchResult(0, 0, 0))

    monkeypatch.setattr(worker, "run_cycle", flaky_cycle)

    with caplog.at_level(logging.ERROR):
        await asyncio.wait_for(
            run_forever(test_sessionmaker, fake_notifier, _settings(interval=1), stop_event),
            timeout=5,
        )

    assert calls == 2
    assert any("cycle_failed" in record.getMessage() for record in caplog.records)


async def test_run_forever_propagates_cancelled_error(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    fake_notifier: FakeNotifier,
    monkeypatch,
) -> None:
    """`CancelledError` no se traga: es la cancelación del proceso."""

    async def cancelled_cycle(sessionmaker, notifier, settings, **kwargs):  # type: ignore[no-untyped-def]
        raise asyncio.CancelledError

    monkeypatch.setattr(worker, "run_cycle", cancelled_cycle)

    with pytest.raises(asyncio.CancelledError):
        await run_forever(
            test_sessionmaker,
            fake_notifier,
            _settings(interval=3600),
            asyncio.Event(),
        )


async def test_the_wait_between_cycles_is_interruptible(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    fake_notifier: FakeNotifier,
    monkeypatch,
) -> None:
    """Con un intervalo enorme, setear el evento corta la espera de inmediato."""
    stop_event = asyncio.Event()
    calls = 0

    async def ok_cycle(sessionmaker, notifier, settings, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return CycleResult(created=0, dispatch=DispatchResult(0, 0, 0))

    monkeypatch.setattr(worker, "run_cycle", ok_cycle)

    async def stop_soon() -> None:
        await asyncio.sleep(0.05)
        stop_event.set()

    stopper = asyncio.create_task(stop_soon())
    started = time.perf_counter()
    try:
        await asyncio.wait_for(
            run_forever(test_sessionmaker, fake_notifier, _settings(interval=3600), stop_event),
            timeout=2,
        )
    finally:
        stopper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stopper

    assert time.perf_counter() - started < 1
    assert stop_event.is_set()
    assert calls == 1


# --- SIGINT/SIGTERM ---------------------------------------------------------


class _FakeLoop:
    """Loop mínimo que registra los handlers de señales."""

    def __init__(self) -> None:
        self.handlers: dict[int, Callable[[], None]] = {}

    def add_signal_handler(self, sig: int, callback: Callable[[], None]) -> None:
        self.handlers[sig] = callback

    def remove_signal_handler(self, sig: int) -> None:
        self.handlers.pop(sig, None)


def test_sigint_and_sigterm_set_the_stop_event() -> None:
    """SIGTERM/SIGINT no matan el proceso: setean el evento y el ciclo en curso termina."""
    stop_event = asyncio.Event()
    loop = _FakeLoop()

    _install_stop_handlers(stop_event, loop)  # type: ignore[arg-type]

    assert set(loop.handlers) == {signal.SIGINT, signal.SIGTERM}
    loop.handlers[signal.SIGTERM]()
    assert stop_event.is_set()


# --- main -------------------------------------------------------------------


def test_main_once_runs_a_single_cycle(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`main(["--once"])` corre un ciclo y sale (sin loop infinito ni DB real)."""
    calls: list[Settings] = []
    stop_events: list[asyncio.Event | None] = []

    async def fake_cycle(sessionmaker, notifier, settings, *, stop_event=None):
        calls.append(settings)
        stop_events.append(stop_event)
        return CycleResult(created=3, dispatch=DispatchResult(0, 0, 0))

    monkeypatch.setattr(worker, "run_cycle", fake_cycle)
    monkeypatch.setattr(worker, "configure_logging", lambda: None)

    worker.main(["--once"])

    assert len(calls) == 1
    # S7 SEC-3: `--once` pasa el evento real de shutdown al ciclo.
    assert len(stop_events) == 1
    assert isinstance(stop_events[0], asyncio.Event)


def test_main_once_hands_the_cycle_the_event_wired_to_the_signals(monkeypatch) -> None:
    """S7 SEC-3: el evento de `--once` es el mismo que setean SIGINT/SIGTERM."""
    installed: list[asyncio.Event] = []
    received: list[asyncio.Event | None] = []

    real_install = worker._install_stop_handlers

    def spy_install(stop_event, loop=None):  # type: ignore[no-untyped-def]
        installed.append(stop_event)
        real_install(stop_event, loop)

    async def fake_cycle(sessionmaker, notifier, settings, *, stop_event=None):  # type: ignore[no-untyped-def]
        received.append(stop_event)
        return CycleResult(created=0, dispatch=DispatchResult(0, 0, 0))

    monkeypatch.setattr(worker, "_install_stop_handlers", spy_install)
    monkeypatch.setattr(worker, "run_cycle", fake_cycle)
    monkeypatch.setattr(worker, "configure_logging", lambda: None)

    worker.main(["--once"])

    assert len(installed) == 1
    assert len(received) == 1
    assert received[0] is installed[0]
    assert isinstance(installed[0], asyncio.Event)


def test_main_once_with_the_stop_event_set_starts_no_dispatch_batch(monkeypatch) -> None:
    """S7 SEC-3: con el `stop_event` seteado, el ciclo de `--once` no arranca ningún lote."""
    batches: list[int] = []

    async def fake_evaluate(session: object, **kwargs: object) -> int:
        return 0

    async def recording_dispatch(sessionmaker, notifier, **kwargs):
        batches.append(1)
        return DispatchResult(sent=0, retried=0, failed=0)

    real_run_cycle = worker.run_cycle

    async def cycle_that_gets_sigterm(sessionmaker, notifier, settings, **kwargs):
        stop_event = kwargs["stop_event"]
        assert isinstance(stop_event, asyncio.Event)
        stop_event.set()  # SIGTERM justo al arrancar el único ciclo
        return await real_run_cycle(sessionmaker, notifier, settings, **kwargs)

    class _NoopSession:
        """Sesión mínima para el ciclo real: solo necesita `commit()` y el contexto async."""

        async def __aenter__(self) -> "_NoopSession":
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            return None

        async def commit(self) -> None:
            return None

    class _FakeEngine:
        async def dispose(self) -> None:
            return None

    def fake_get_engine() -> _FakeEngine:
        return _FakeEngine()

    fake_get_engine.cache_clear = lambda: None  # type: ignore[attr-defined]

    def fake_get_sessionmaker() -> Callable[[], _NoopSession]:
        return _NoopSession

    fake_get_sessionmaker.cache_clear = lambda: None  # type: ignore[attr-defined]

    monkeypatch.setattr(worker, "evaluate_alerts", fake_evaluate)
    monkeypatch.setattr(worker, "dispatch_pending", recording_dispatch)
    monkeypatch.setattr(worker, "run_cycle", cycle_that_gets_sigterm)
    monkeypatch.setattr(worker, "get_engine", fake_get_engine)
    monkeypatch.setattr(worker, "get_sessionmaker", fake_get_sessionmaker)
    monkeypatch.setattr(worker, "get_settings", lambda: _settings())
    monkeypatch.setattr(worker, "configure_logging", lambda: None)

    worker.main(["--once"])

    assert batches == []


def test_main_once_disposes_the_engine_when_the_cycle_fails(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Aunque `run_cycle` falle, `main(["--once"])` propaga el error pero libera el engine."""
    disposed: list[bool] = []

    class _FakeEngine:
        async def dispose(self) -> None:
            disposed.append(True)

    def fake_get_engine() -> _FakeEngine:
        return _FakeEngine()

    fake_get_engine.cache_clear = lambda: None  # type: ignore[attr-defined]
    sessionmaker = object()

    def fake_get_sessionmaker() -> object:
        return sessionmaker

    fake_get_sessionmaker.cache_clear = lambda: None  # type: ignore[attr-defined]

    async def failing_cycle(smaker, notifier, settings, **kwargs):  # type: ignore[no-untyped-def]
        assert smaker is sessionmaker
        raise RuntimeError("boom")

    monkeypatch.setattr(worker, "run_cycle", failing_cycle)
    monkeypatch.setattr(worker, "get_engine", fake_get_engine)
    monkeypatch.setattr(worker, "get_sessionmaker", fake_get_sessionmaker)
    monkeypatch.setattr(worker, "get_settings", lambda: _settings())
    monkeypatch.setattr(worker, "configure_logging", lambda: None)

    with pytest.raises(RuntimeError, match="boom"):
        worker.main(["--once"])

    assert disposed == [True]


async def test_run_cycle_evaluates_with_now_and_the_freshness_setting(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    fake_notifier: FakeNotifier,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El worker le pasa al evaluador su `now` y los settings de frescura/horizonte (S11/S12a)."""
    captured: dict[str, object] = {}

    async def _fake_evaluate(session: AsyncSession, **kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(worker, "evaluate_alerts", _fake_evaluate)
    settings = Settings(
        dispatch_batch_size=10,
        dispatch_max_attempts=3,
        dispatch_backoff_base_seconds=30,
        notifier_timeout_seconds=1.0,
        dispatch_concurrency=10,
        dispatch_lease_seconds=60,
        max_forecast_age_hours=6,
        forecast_future_tolerance_minutes=30,
        alert_horizon_days=3,
        rearm_margin=15,
    )

    await run_cycle(
        test_sessionmaker,
        fake_notifier,
        settings,
        now=EVAL_NOW,
        clock=_clock_at(CLAIM_NOW),
    )

    assert captured == {
        "now": EVAL_NOW,
        "horizon_days": 3,
        "max_forecast_age_hours": 6,
        "future_tolerance_minutes": 30,
        "rearm_margin": 15,
    }


async def test_one_cycle_evaluates_each_field_with_its_own_local_today(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    make_field: MakeField,
    make_forecast: MakeForecast,
    make_alert: MakeAlert,
    fake_notifier: FakeNotifier,
) -> None:
    """Dos campos en +14 y -11 en el mismo ciclo: cada uno evalúa su propio "hoy" (D9).

    Con horizonte 1, a las 12:00Z del 2030-01-01 Kiritimati ya está en el 2 y Pago Pago en el
    1. Los dos campos tienen los mismos pronósticos guardados, así que el ciclo debe crear y
    enviar exactamente un aviso por campo, cada uno para su día local.
    """
    east = await make_field(name="Lote +14")
    east.timezone = "Pacific/Kiritimati"
    west = await make_field(name="Lote -11")
    west.timezone = "Pacific/Pago_Pago"
    await db_session.flush()
    alerts = {}
    for field in (east, west):
        alert = await make_alert(field, event_type=EventType.FROST, threshold=50)
        alerts[field.id] = alert
        for day in (date(2030, 1, 1), date(2030, 1, 2)):
            await make_forecast(
                field,
                forecast_date=day,
                event_type=EventType.FROST,
                probability=85,
                issued_at=EVAL_NOW,
            )
    settings = _settings().model_copy(update={"alert_horizon_days": 1})

    result = await run_cycle(
        test_sessionmaker,
        fake_notifier,
        settings,
        now=EVAL_NOW,
        clock=_clock_at(CLAIM_NOW),
    )

    assert result == CycleResult(
        created=2, dispatch=DispatchResult(sent=2, retried=0, failed=0, users=2)
    )
    notifications = list((await db_session.execute(select(Notification))).scalars().all())
    rows = {(notification.alert_id, notification.forecast_date) for notification in notifications}
    assert rows == {
        (alerts[east.id].id, date(2030, 1, 2)),
        (alerts[west.id].id, date(2030, 1, 1)),
    }
    assert {notification.status for notification in notifications} == {NotificationStatus.SENT}
    assert len(fake_notifier.sent) == 2


# --- S13: envío agrupado, digest y horario de silencio ----------------------


async def test_run_cycle_passes_digest_max_items_to_the_dispatcher(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    fake_notifier: FakeNotifier,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def recording_dispatch(sessionmaker, notifier, **kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return DispatchResult(sent=0, retried=0, failed=0)

    monkeypatch.setattr(worker, "dispatch_pending", recording_dispatch)
    settings = _settings(batch_size=3).model_copy(update={"digest_max_items": 4})

    await run_cycle(
        test_sessionmaker,
        fake_notifier,
        settings,
        now=EVAL_NOW,
        clock=_clock_at(CLAIM_NOW),
    )

    assert captured["digest_max_items"] == 4
    assert captured["batch_size"] == 3


async def test_run_cycle_keeps_draining_while_the_batch_comes_full(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    make_field: MakeField,
    make_forecast: MakeForecast,
    make_alert: MakeAlert,
    fake_notifier: FakeNotifier,
) -> None:
    """`batch_size=1` y 3 avisos del mismo usuario con `digest_max_items=2`: 2 lotes (2+1).

    El lote vino lleno (1 usuario == batch_size) aunque no quedaran notificaciones: el worker
    sigue drenando; el excedente del digest sale en el lote siguiente.
    """
    field = await make_field()
    await make_alert(field, event_type=EventType.FROST, threshold=70)
    for offset in range(3):
        await make_forecast(
            field,
            forecast_date=FORECAST_DATE + timedelta(days=offset),
            event_type=EventType.FROST,
            probability=90,
            issued_at=EVAL_NOW,
        )
    settings = _settings(batch_size=1).model_copy(update={"digest_max_items": 2})

    result = await run_cycle(
        test_sessionmaker,
        fake_notifier,
        settings,
        now=EVAL_NOW,
        clock=_clock_at(CLAIM_NOW),
    )

    assert result.created == 3
    assert result.dispatch == DispatchResult(sent=3, retried=0, failed=0, users=2)
    # Un mensaje con 2 avisos y otro con el que quedó para el lote siguiente.
    assert len(fake_notifier.sent) == 2
    assert fake_notifier.sent[0].text.startswith("⚠️ Tenés 2 alertas climáticas:")
    assert "Tenés" not in fake_notifier.sent[1].text
    statuses = (await db_session.scalars(select(Notification.status))).all()
    assert statuses.count(NotificationStatus.SENT) == 3


async def test_run_cycle_defers_during_quiet_hours_and_sends_a_digest_when_it_ends(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    make_user: MakeUser,
    make_field: MakeField,
    make_forecast: MakeForecast,
    make_alert: MakeAlert,
    fake_notifier: FakeNotifier,
) -> None:
    """S13/D15: en silencio no se envía nada; al terminar salen todas juntas en un mensaje."""
    owner = await make_user()
    owner.quiet_hours_start = dt_time(8, 0)
    owner.quiet_hours_end = dt_time(10, 0)
    await db_session.flush()
    field = await make_field(owner, name="Lote Norte")
    await make_alert(field, event_type=EventType.FROST, threshold=70)
    for offset in range(2):
        await make_forecast(
            field,
            forecast_date=FORECAST_DATE + timedelta(days=offset),
            event_type=EventType.FROST,
            probability=90,
            issued_at=EVAL_NOW,
        )

    # 12:01Z del 01/01/2030 = 09:01 local: adentro de la ventana 08:00-10:00.
    deferred = await run_cycle(
        test_sessionmaker,
        fake_notifier,
        _settings(),
        now=EVAL_NOW,
        clock=_clock_at(CLAIM_NOW),
    )

    assert deferred == CycleResult(
        created=2,
        dispatch=DispatchResult(sent=0, retried=0, failed=0, deferred=2, users=1),
    )
    assert fake_notifier.sent == []
    quiet_until_local_end = datetime(2030, 1, 1, 13, 0, tzinfo=UTC)  # 10:00 local
    for notification in (await db_session.scalars(select(Notification))).all():
        assert notification.status == NotificationStatus.PENDING
        assert notification.next_attempt_at == quiet_until_local_end
        assert notification.attempts == 0

    sent = await run_cycle(
        test_sessionmaker,
        fake_notifier,
        _settings(),
        now=datetime(2030, 1, 1, 12, 0, tzinfo=UTC),
        clock=_clock_at(quiet_until_local_end),
    )

    assert sent == CycleResult(
        created=0,
        dispatch=DispatchResult(sent=2, retried=0, failed=0, users=1),
    )
    assert len(fake_notifier.sent) == 1
    assert fake_notifier.sent[0].text.startswith("⚠️ Tenés 2 alertas climáticas:")
    statuses = (await db_session.scalars(select(Notification.status))).all()
    assert statuses.count(NotificationStatus.SENT) == 2
