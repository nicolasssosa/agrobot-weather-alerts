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

#: Día evaluado (fijo).
EVAL_NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
#: Hora de claim (fija y posterior a cualquier `now()` real del test): las notificaciones
#: creadas por el evaluador con la hora de la DB ya están vencidas.
CLAIM_NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
FORECAST_DATE = date(2026, 11, 18)


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
    )
    # Bajo el umbral: no debe generar notificación.
    await make_forecast(
        field,
        forecast_date=FORECAST_DATE + timedelta(days=1),
        event_type=EventType.FROST,
        probability=10,
    )

    result = await run_cycle(
        test_sessionmaker,
        fake_notifier,
        _settings(),
        now=EVAL_NOW,
        clock=_clock_at(CLAIM_NOW),
    )

    assert result == CycleResult(created=1, dispatch=DispatchResult(sent=1, retried=0, failed=0))
    notification = await db_session.scalar(select(Notification))
    assert notification is not None
    assert fake_notifier.sent == [
        NotificationMessage(
            notification_id=notification.id,
            phone_number="+5491100000001",
            text=(
                '⚠️ Alerta de helada en "Lote Norte": 85% de probabilidad '
                "para el 18/11/2026 (tu umbral: 70%)."
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
    make_field: MakeField,
    make_forecast: MakeForecast,
    make_alert: MakeAlert,
    fake_notifier: FakeNotifier,
    monkeypatch,
) -> None:
    """Con `batch_size=2` y 5 pendientes se hacen 3 lotes, cada uno con su propio `now`."""
    field = await make_field()
    await make_alert(field, event_type=EventType.FROST, threshold=70)
    for offset in range(5):
        await make_forecast(
            field,
            forecast_date=FORECAST_DATE + timedelta(days=offset),
            event_type=EventType.FROST,
            probability=90,
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
    assert result.dispatch == DispatchResult(sent=5, retried=0, failed=0)
    assert len(fake_notifier.sent) == 5
    assert len(recorded) == 3
    # Un `now` fresco y distinto por lote (SEC-3), en orden creciente.
    assert recorded == sorted(recorded)
    assert len(set(recorded)) == 3


# --- Protección anti-loop del drenado --------------------------------------


async def test_run_cycle_does_not_start_a_new_batch_after_stop_event(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    make_field: MakeField,
    make_forecast: MakeForecast,
    make_alert: MakeAlert,
) -> None:
    """Si el notifier setea `stop_event` en el primer lote, no se arranca el segundo (SEC-2)."""
    field = await make_field()
    await make_alert(field, event_type=EventType.FROST, threshold=70)
    for offset in range(4):
        await make_forecast(
            field,
            forecast_date=FORECAST_DATE + timedelta(days=offset),
            event_type=EventType.FROST,
            probability=90,
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

    # Solo se envió el primer lote; las 2 pendientes quedan para el próximo arranque.
    assert result.created == 4
    assert result.dispatch == DispatchResult(sent=2, retried=0, failed=0)
    assert len(notifier.sent) == 2
    statuses = (await db_session.scalars(select(Notification.status))).all()
    assert statuses.count(NotificationStatus.SENT) == 2
    assert statuses.count(NotificationStatus.PENDING) == 2


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


async def test_run_cycle_accumulates_cancelled_and_counts_them_as_processed(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    fake_notifier: FakeNotifier,
    monkeypatch,
    caplog,
) -> None:
    """S9: `cancelled` se acumula, se loguea y un lote lleno de canceladas no corta el drenado."""
    calls = 0

    async def cancelling_then_sending(sessionmaker, notifier, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            return DispatchResult(sent=0, retried=0, failed=0, cancelled=kwargs["batch_size"])
        return DispatchResult(sent=1, retried=0, failed=0)

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
    assert result.dispatch == DispatchResult(sent=1, retried=0, failed=0, cancelled=2)
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

    async def failing_evaluate(session, *, today):  # type: ignore[no-untyped-def]
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
    make_field: MakeField,
    make_forecast: MakeForecast,
    make_alert: MakeAlert,
    fake_notifier: FakeNotifier,
    monkeypatch,
) -> None:
    """Un lote que lanza no pierde lo ya evaluado/commiteado; el ciclo siguiente lo drena."""
    field = await make_field()
    await make_alert(field, event_type=EventType.FROST, threshold=70)
    for offset in range(5):
        await make_forecast(
            field,
            forecast_date=FORECAST_DATE + timedelta(days=offset),
            event_type=EventType.FROST,
            probability=90,
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

    assert second == CycleResult(created=0, dispatch=DispatchResult(sent=3, retried=0, failed=0))
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
        return DispatchResult(sent=kwargs["batch_size"], retried=0, failed=0)

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
        sent=MAX_DISPATCH_BATCHES_PER_CYCLE * 2, retried=0, failed=0
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

    async def fake_cycle(sessionmaker, notifier, settings):  # type: ignore[no-untyped-def]
        calls.append(settings)
        return CycleResult(created=3, dispatch=DispatchResult(0, 0, 0))

    monkeypatch.setattr(worker, "run_cycle", fake_cycle)
    monkeypatch.setattr(worker, "configure_logging", lambda: None)

    worker.main(["--once"])

    assert len(calls) == 1


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

    async def failing_cycle(smaker, notifier, settings):  # type: ignore[no-untyped-def]
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
