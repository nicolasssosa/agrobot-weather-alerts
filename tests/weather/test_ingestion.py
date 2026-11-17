"""Tests de la ingesta mock. Ver docs/TESTING.md §test_ingestion y docs/JOBS.md §Ingesta mock."""

import asyncio
import logging
import random
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.models import EventType, Field, User, WeatherForecast
from app.weather import ingestion as ingestion_module
from app.weather.ingestion import ingest_forecasts as _ingest_forecasts
from app.weather.provider import ForecastPoint, MockWeatherProvider, WeatherProvider
from tests.conftest import TEST_DATABASE_URL, MakeField, MakeForecast, MakeUser

START = date(2026, 11, 16)
#: Corridas del modelo inyectadas: nunca el reloj real (S11).
ISSUED_AT = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 11, 16, 13, 0, tzinfo=UTC)
EARLIER = datetime(2026, 11, 16, 11, 0, tzinfo=UTC)
#: Corrida de la ingesta inyectada: nunca el reloj real (S11 SEC-1).
NOW = LATER
FUTURE_TOLERANCE_MINUTES = 60


async def ingest_forecasts(
    session: AsyncSession,
    provider: WeatherProvider,
    *,
    start: date,
    days: int,
    source: str = "mock",
    field_ids: Sequence[int] | None = None,
    now: datetime = NOW,
    future_tolerance_minutes: int = FUTURE_TOLERANCE_MINUTES,
) -> int:
    """Llama a la ingesta real con una corrida fija (`now`), nunca el reloj de pared."""
    return await _ingest_forecasts(
        session,
        provider,
        start=start,
        days=days,
        source=source,
        field_ids=field_ids,
        now=now,
        future_tolerance_minutes=future_tolerance_minutes,
    )


async def _rows(session: AsyncSession, field_id: int) -> list[WeatherForecast]:
    result = await session.execute(
        select(WeatherForecast)
        .where(WeatherForecast.field_id == field_id)
        .order_by(WeatherForecast.forecast_date, WeatherForecast.event_type)
        # Refresca la identidad: el upsert es Core y no actualiza los objetos ya cargados.
        .execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


class _RecordingProvider(MockWeatherProvider):
    """Provider que registra los ids de los campos que le piden pronósticos."""

    def __init__(self, seed: int = 42) -> None:
        super().__init__(seed)
        self.fetched_field_ids: list[int] = []

    async def fetch(self, field: Field, *, start: date, days: int) -> list[ForecastPoint]:
        self.fetched_field_ids.append(field.id)
        return await super().fetch(field, start=start, days=days)


async def test_mock_provider_returns_one_point_per_day_and_event_type(
    make_field: MakeField,
) -> None:
    field = await make_field()

    points = await MockWeatherProvider(seed=7).fetch(field, start=START, days=3)

    assert len(points) == 3 * len(EventType)
    assert {point.forecast_date for point in points} == {
        START + timedelta(days=offset) for offset in range(3)
    }
    assert {point.event_type for point in points} == set(EventType)
    assert all(point.field_id == field.id for point in points)
    assert all(0 <= point.probability <= 100 for point in points)


async def test_mock_provider_is_deterministic(make_field: MakeField) -> None:
    field = await make_field()

    first = await MockWeatherProvider(seed=42).fetch(field, start=START, days=5)
    second = await MockWeatherProvider(seed=42).fetch(field, start=START, days=5)

    assert first == second


async def test_mock_provider_matches_the_documented_seed_formula(
    make_field: MakeField,
) -> None:
    field = await make_field()

    points = await MockWeatherProvider(seed=42).fetch(field, start=START, days=2)

    for point in points:
        expected = random.Random(
            f"42:{field.id}:{point.forecast_date.isoformat()}:{point.event_type.value}"
        ).randint(0, 100)
        assert point.probability == expected


async def test_mock_provider_changes_with_the_seed(make_field: MakeField) -> None:
    field = await make_field()

    first = await MockWeatherProvider(seed=1).fetch(field, start=START, days=1)
    second = await MockWeatherProvider(seed=2).fetch(field, start=START, days=1)

    assert [point.probability for point in first] != [point.probability for point in second]


async def test_mock_provider_with_zero_days_returns_nothing(make_field: MakeField) -> None:
    field = await make_field()

    assert await MockWeatherProvider().fetch(field, start=START, days=0) == []


async def test_mock_provider_issues_at_the_truncated_model_run(make_field: MakeField) -> None:
    """Todos los puntos comparten `issued_at`: la corrida inyectada, truncada a la hora (S11)."""
    field = await make_field()
    run_at = datetime(2026, 11, 16, 12, 47, 33, 500, tzinfo=UTC)

    points = await MockWeatherProvider(now=run_at).fetch(field, start=START, days=2)

    assert {point.issued_at for point in points} == {datetime(2026, 11, 16, 12, 0, tzinfo=UTC)}


async def test_mock_provider_normalizes_the_run_to_utc(make_field: MakeField) -> None:
    """Una corrida inyectada en otra zona horaria se expresa en UTC antes de truncar."""
    field = await make_field()
    run_at = datetime(2026, 11, 16, 14, 47, tzinfo=timezone(timedelta(hours=2)))

    points = await MockWeatherProvider(now=run_at).fetch(field, start=START, days=1)

    assert {point.issued_at for point in points} == {datetime(2026, 11, 16, 12, 0, tzinfo=UTC)}


async def test_mock_provider_uses_the_model_run_for_every_fetch(make_field: MakeField) -> None:
    """El mismo provider conserva su corrida (determinístico, sin reloj real)."""
    field = await make_field()
    provider = MockWeatherProvider(now=ISSUED_AT)

    first = await provider.fetch(field, start=START, days=1)
    second = await provider.fetch(field, start=START, days=1)

    assert {point.issued_at for point in first + second} == {ISSUED_AT}


async def test_ingest_inserts_a_row_per_day_and_event_type(
    db_session: AsyncSession, make_field: MakeField
) -> None:
    field = await make_field()

    processed = await ingest_forecasts(db_session, MockWeatherProvider(), start=START, days=3)

    assert processed == 3 * len(EventType)
    rows = await _rows(db_session, field.id)
    assert len(rows) == 3 * len(EventType)
    assert {row.forecast_date for row in rows} == {
        START + timedelta(days=offset) for offset in range(3)
    }
    assert {row.event_type for row in rows} == set(EventType)
    assert all(row.source == "mock" for row in rows)
    assert all(0 <= row.probability <= 100 for row in rows)
    assert all(row.fetched_at is not None for row in rows)


async def test_ingest_covers_every_field(db_session: AsyncSession, make_field: MakeField) -> None:
    fields = [await make_field() for _ in range(3)]

    processed = await ingest_forecasts(db_session, MockWeatherProvider(), start=START, days=2)

    assert processed == 3 * 2 * len(EventType)
    total = await db_session.scalar(select(func.count()).select_from(WeatherForecast))
    assert total == 3 * 2 * len(EventType)
    for field in fields:
        assert len(await _rows(db_session, field.id)) == 2 * len(EventType)


async def test_reingesting_updates_rows_without_duplicating(
    db_session: AsyncSession, make_field: MakeField
) -> None:
    field = await make_field()

    first = await ingest_forecasts(
        db_session,
        MockWeatherProvider(seed=1, now=ISSUED_AT),
        start=START,
        days=2,
        source="mock",
    )
    second = await ingest_forecasts(
        db_session,
        MockWeatherProvider(seed=2, now=LATER),
        start=START,
        days=2,
        source="fresh",
    )

    assert first == second == 2 * len(EventType)
    rows = await _rows(db_session, field.id)
    assert len(rows) == 2 * len(EventType)
    assert all(row.source == "fresh" for row in rows)
    assert all(row.issued_at == LATER for row in rows)
    expected = {
        (START + timedelta(days=offset), event_type): random.Random(
            f"2:{field.id}:{(START + timedelta(days=offset)).isoformat()}:{event_type.value}"
        ).randint(0, 100)
        for offset in range(2)
        for event_type in EventType
    }
    assert {(row.forecast_date, row.event_type): row.probability for row in rows} == expected


async def test_ingest_paginates_fields_by_id(
    db_session: AsyncSession,
    make_field: MakeField,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ingestion_module, "_BATCH_SIZE", 2)
    fields = [await make_field() for _ in range(5)]
    provider = _RecordingProvider()

    processed = await ingest_forecasts(db_session, provider, start=START, days=1)

    assert processed == 5 * len(EventType)
    # Recorre los 5 campos en orden de id, sin repetir ni saltear ninguno (3 páginas de a 2).
    assert provider.fetched_field_ids == [field.id for field in fields]
    assert len(provider.fetched_field_ids) == len(set(provider.fetched_field_ids))


async def test_ingest_with_field_ids_only_touches_the_requested_fields(
    db_session: AsyncSession, make_field: MakeField, make_forecast: MakeForecast
) -> None:
    """Con `field_ids`, solo se ingesta ese campo y los demás quedan intactos (S7 SEC-1)."""
    wanted = await make_field()
    other = await make_field()
    existing = await make_forecast(
        other,
        forecast_date=START,
        event_type=EventType.RAIN,
        probability=7,
        source="manual",
    )

    processed = await ingest_forecasts(
        db_session,
        MockWeatherProvider(),
        start=START,
        days=2,
        field_ids=[wanted.id],
    )

    assert processed == 2 * len(EventType)
    assert len(await _rows(db_session, wanted.id)) == 2 * len(EventType)
    await db_session.refresh(existing)
    assert existing.probability == 7
    assert existing.source == "manual"
    # El campo ajeno conserva únicamente su fila preexistente: la ingesta no lo tocó.
    assert len(await _rows(db_session, other.id)) == 1


async def test_ingest_with_field_ids_keeps_the_keyset_pagination(
    db_session: AsyncSession,
    make_field: MakeField,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La paginación keyset sigue funcionando restringida a los `field_ids` pedidos."""
    monkeypatch.setattr(ingestion_module, "_BATCH_SIZE", 2)
    fields = [await make_field() for _ in range(5)]
    wanted_ids = [fields[0].id, fields[2].id, fields[4].id]
    provider = _RecordingProvider()

    processed = await ingest_forecasts(
        db_session, provider, start=START, days=1, field_ids=wanted_ids
    )

    assert processed == len(wanted_ids) * len(EventType)
    assert provider.fetched_field_ids == wanted_ids
    for field in fields:
        expected = len(EventType) if field.id in wanted_ids else 0
        assert len(await _rows(db_session, field.id)) == expected


async def test_ingest_leaves_forecasts_outside_the_requested_range_untouched(
    db_session: AsyncSession, make_field: MakeField, make_forecast: MakeForecast
) -> None:
    field = await make_field()
    outside = await make_forecast(
        field,
        forecast_date=START + timedelta(days=30),
        event_type=EventType.RAIN,
        probability=3,
        source="manual",
    )

    await ingest_forecasts(db_session, MockWeatherProvider(), start=START, days=2)

    await db_session.refresh(outside)
    assert outside.probability == 3
    assert outside.source == "manual"


async def test_ingest_with_zero_days_inserts_nothing(
    db_session: AsyncSession, make_field: MakeField
) -> None:
    field = await make_field()

    processed = await ingest_forecasts(db_session, MockWeatherProvider(), start=START, days=0)

    assert processed == 0
    assert await _rows(db_session, field.id) == []


async def test_ingest_without_fields_returns_zero(db_session: AsyncSession) -> None:
    assert await ingest_forecasts(db_session, MockWeatherProvider(), start=START, days=3) == 0


async def test_ingest_with_one_day_inserts_only_that_day(
    db_session: AsyncSession, make_field: MakeField
) -> None:
    field = await make_field()

    processed = await ingest_forecasts(db_session, MockWeatherProvider(), start=START, days=1)

    assert processed == len(EventType)
    rows = await _rows(db_session, field.id)
    assert len(rows) == len(EventType)
    assert {row.forecast_date for row in rows} == {START}
    assert {row.event_type for row in rows} == set(EventType)


async def test_reingest_updates_source_and_fetched_at_but_not_created_at(
    db_session: AsyncSession, make_field: MakeField
) -> None:
    field = await make_field()
    old = datetime(2020, 1, 1, tzinfo=UTC)
    existing = WeatherForecast(
        field_id=field.id,
        forecast_date=START,
        event_type=EventType.FROST,
        probability=1,
        source="manual",
        issued_at=old,
        fetched_at=old,
    )
    existing.created_at = old
    existing.updated_at = old
    db_session.add(existing)
    await db_session.flush()

    await ingest_forecasts(
        db_session, MockWeatherProvider(seed=3, now=ISSUED_AT), start=START, days=1
    )

    await db_session.refresh(existing)
    assert existing.probability == random.Random(
        f"3:{field.id}:{START.isoformat()}:{EventType.FROST.value}"
    ).randint(0, 100)
    assert existing.source == "mock"
    assert existing.issued_at == ISSUED_AT
    assert existing.fetched_at > old
    assert existing.updated_at > old
    # El upsert no debe reescribir `created_at` (es la fecha de alta de la fila).
    assert existing.created_at == old


async def test_a_field_created_between_two_runs_is_ingested(
    db_session: AsyncSession, make_field: MakeField
) -> None:
    first = await make_field()
    await ingest_forecasts(db_session, MockWeatherProvider(), start=START, days=1)

    second = await make_field()
    await ingest_forecasts(db_session, MockWeatherProvider(), start=START, days=1)

    assert len(await _rows(db_session, first.id)) == len(EventType)
    assert len(await _rows(db_session, second.id)) == len(EventType)


async def test_ingest_does_not_commit(db_session: AsyncSession, make_field: MakeField) -> None:
    field = await make_field()

    await ingest_forecasts(db_session, MockWeatherProvider(), start=START, days=1)
    assert len(await _rows(db_session, field.id)) == len(EventType)

    # Si `ingest_forecasts` hubiera commiteado, las filas sobrevivirían al rollback de la
    # sesión (el commit del test se convierte en un SAVEPOINT ya liberado).
    await db_session.rollback()
    assert await db_session.scalar(select(func.count()).select_from(WeatherForecast)) == 0


async def test_ingest_of_a_full_page_with_the_default_horizon(
    db_session: AsyncSession, make_user: MakeUser
) -> None:
    user = await make_user()
    db_session.add_all(
        [
            Field(
                user_id=user.id,
                name=f"Scale {index}",
                latitude=Decimal("-34.6"),
                longitude=Decimal("-58.4"),
            )
            for index in range(ingestion_module._BATCH_SIZE)
        ]
    )
    await db_session.flush()

    processed = await ingest_forecasts(db_session, MockWeatherProvider(), start=START, days=14)

    assert processed == ingestion_module._BATCH_SIZE * 14 * len(EventType)
    total = await db_session.scalar(select(func.count()).select_from(WeatherForecast))
    assert total == ingestion_module._BATCH_SIZE * 14 * len(EventType)


async def test_upsert_chunks_rows_to_stay_below_the_parameter_limit(
    db_session: AsyncSession,
    make_field: MakeField,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fields = [await make_field() for _ in range(2)]
    provider = MockWeatherProvider(seed=11, now=ISSUED_AT)
    # Cada fila bindea 7 columnas: con `_MAX_PARAMS = 14` cada statement lleva 2 filas.
    monkeypatch.setattr(ingestion_module, "_MAX_PARAMS", 14)
    original = ingestion_module._upsert_chunk
    chunks: list[int] = []

    async def _spy(session: AsyncSession, rows: Sequence[dict[str, object]]) -> int:
        chunks.append(len(rows))
        return await original(session, rows)

    monkeypatch.setattr(ingestion_module, "_upsert_chunk", _spy)

    processed = await ingest_forecasts(db_session, provider, start=START, days=1)

    assert processed == len(fields) * len(EventType)
    # 10 filas en chunks de 2: 5 statements, todos dentro de la misma transacción.
    assert chunks == [2] * (processed // 2)
    stored = {
        (row.field_id, row.forecast_date, row.event_type): row.probability
        for field in fields
        for row in await _rows(db_session, field.id)
    }
    expected = {
        (point.field_id, point.forecast_date, point.event_type): point.probability
        for field in fields
        for point in await provider.fetch(field, start=START, days=1)
    }
    # El troceo no cambia el resultado final respecto de un único INSERT.
    assert stored == expected


# --- Calidad de pronósticos: solo un dato más nuevo pisa al guardado (S11, D14) ---


def _stored_state(
    rows: Sequence[WeatherForecast],
) -> dict[tuple[date, EventType], tuple[int, datetime, str]]:
    """Snapshot de lo que no debe cambiar cuando el dato entrante es igual o más viejo."""
    return {
        (row.forecast_date, row.event_type): (row.probability, row.issued_at, row.source)
        for row in rows
    }


async def test_a_newer_issue_updates_the_stored_forecast(
    db_session: AsyncSession, make_field: MakeField
) -> None:
    """Un pronóstico emitido después actualiza probabilidad, fuente e `issued_at`."""
    field = await make_field()
    await ingest_forecasts(
        db_session, MockWeatherProvider(seed=1, now=ISSUED_AT), start=START, days=1
    )

    written = await ingest_forecasts(
        db_session,
        MockWeatherProvider(seed=2, now=LATER),
        start=START,
        days=1,
        source="fresh",
    )

    assert written == len(EventType)
    rows = await _rows(db_session, field.id)
    assert all(row.issued_at == LATER for row in rows)
    assert all(row.source == "fresh" for row in rows)
    assert _stored_state(rows) == {
        (START, event_type): (
            random.Random(f"2:{field.id}:{START.isoformat()}:{event_type.value}").randint(0, 100),
            LATER,
            "fresh",
        )
        for event_type in EventType
    }


async def test_an_equal_issue_does_not_overwrite_and_is_not_counted(
    db_session: AsyncSession, make_field: MakeField
) -> None:
    """Reingestar la misma corrida del modelo no pisa el dato guardado ni cuenta como escrito."""
    field = await make_field()
    await ingest_forecasts(
        db_session, MockWeatherProvider(seed=1, now=ISSUED_AT), start=START, days=1
    )
    before = _stored_state(await _rows(db_session, field.id))

    written = await ingest_forecasts(
        db_session,
        MockWeatherProvider(seed=2, now=ISSUED_AT),
        start=START,
        days=1,
        source="late",
    )

    assert written == 0
    assert _stored_state(await _rows(db_session, field.id)) == before


async def test_an_older_issue_does_not_overwrite_and_is_not_counted(
    db_session: AsyncSession, make_field: MakeField
) -> None:
    """Un lote atrasado (emitido antes) no retrocede probabilidad ni `issued_at`."""
    field = await make_field()
    await ingest_forecasts(db_session, MockWeatherProvider(seed=1, now=LATER), start=START, days=1)
    before = _stored_state(await _rows(db_session, field.id))

    written = await ingest_forecasts(
        db_session,
        MockWeatherProvider(seed=2, now=EARLIER),
        start=START,
        days=1,
        source="late",
    )

    assert written == 0
    assert _stored_state(await _rows(db_session, field.id)) == before


async def test_only_the_newer_rows_of_a_mixed_batch_are_written(
    db_session: AsyncSession, make_field: MakeField, make_forecast: MakeForecast
) -> None:
    """En una misma corrida conviven filas nuevas y filas que ya tienen un dato más nuevo."""
    field = await make_field()
    fresh_row = await make_forecast(
        field,
        forecast_date=START,
        event_type=EventType.FROST,
        probability=1,
        issued_at=LATER,
        source="manual",
    )

    written = await ingest_forecasts(
        db_session, MockWeatherProvider(seed=3, now=ISSUED_AT), start=START, days=1
    )

    # Las `len(EventType) - 1` filas restantes se insertan; `frost` queda intacto.
    assert written == len(EventType) - 1
    await db_session.refresh(fresh_row)
    assert (fresh_row.probability, fresh_row.issued_at, fresh_row.source) == (1, LATER, "manual")


async def test_skipped_older_forecasts_are_logged(
    db_session: AsyncSession,
    make_field: MakeField,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Sólo se loguea `forecasts_skipped_older` cuando hay datos descartados por viejos."""
    field = await make_field()
    caplog.set_level(logging.INFO, logger="app.weather.ingestion")

    await ingest_forecasts(db_session, MockWeatherProvider(seed=1, now=LATER), start=START, days=1)
    assert "forecasts_skipped_older" not in caplog.text

    caplog.clear()
    await ingest_forecasts(
        db_session,
        MockWeatherProvider(seed=2, now=EARLIER),
        start=START,
        days=1,
        source="late",
    )

    assert f"forecasts_skipped_older count={len(EventType)}" in caplog.text
    assert all(row.issued_at == LATER for row in await _rows(db_session, field.id))


async def test_a_mixed_batch_crossing_chunk_boundaries_counts_exactly(
    db_session: AsyncSession,
    make_field: MakeField,
    make_forecast: MakeForecast,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mezcla de datos más nuevos / iguales / ausentes en un lote troceado (S11).

    Con `_MAX_PARAMS = 14` cada statement lleva 2 filas, así que las dos preexistentes caen
    en chunks distintos: una más nueva y otra con el mismo `issued_at`. Solo se cuentan las
    filas realmente escritas (`len(filas) - 2`), sin importar en qué chunk caiga cada una.
    """
    fields = [await make_field(), await make_field()]
    newest = datetime(2099, 1, 1, tzinfo=UTC)
    # `frost` del primer campo tiene un dato más nuevo; `hail` del mismo campo ya está en la
    # corrida que se ingesta (chunk 2 con `_MAX_PARAMS = 14`).
    newer_row = await make_forecast(
        fields[0],
        forecast_date=START,
        event_type=EventType.FROST,
        probability=1,
        source="manual-newer",
        issued_at=newest,
    )
    equal_row = await make_forecast(
        fields[0],
        forecast_date=START,
        event_type=EventType.HAIL,
        probability=2,
        source="manual-equal",
        issued_at=ISSUED_AT,
    )
    monkeypatch.setattr(ingestion_module, "_MAX_PARAMS", 14)

    written = await ingest_forecasts(
        db_session, MockWeatherProvider(seed=7, now=ISSUED_AT), start=START, days=1
    )

    attempted = len(fields) * len(EventType)
    assert written == attempted - 2
    # Las dos filas preexistentes quedaron intactas (una más nueva y una igual).
    await db_session.refresh(newer_row)
    await db_session.refresh(equal_row)
    assert (newer_row.probability, newer_row.issued_at, newer_row.source) == (
        1,
        newest,
        "manual-newer",
    )
    assert (equal_row.probability, equal_row.issued_at, equal_row.source) == (
        2,
        ISSUED_AT,
        "manual-equal",
    )
    # El resto se insertó con la corrida del lote, también en el campo completo.
    for field in fields:
        rows = await _rows(db_session, field.id)
        assert len(rows) == len(EventType)
        preserved = 2 if field.id == fields[0].id else 0
        assert sum(row.source != "mock" for row in rows) == preserved
        for row in rows:
            if row.id != newer_row.id:
                assert row.issued_at == ISSUED_AT


# --- Concurrencia: la corrida más nueva gana sin importar el orden de commit (S11) -


async def _new_concurrent_setup() -> tuple[AsyncEngine, async_sessionmaker[AsyncSession], int, int]:
    """Crea usuario + campo commiteados en su propia conexión y devuelve la factory."""
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as setup:
        user = User(
            phone_number=f"+54911{uuid.uuid4().int % 10**8:08d}",
            name=f"QA ingest concurrent {uuid.uuid4().hex}",
        )
        setup.add(user)
        await setup.flush()
        field = Field(
            user_id=user.id,
            name=f"QA ingest field {uuid.uuid4().hex}",
            latitude=Decimal("-34.6"),
            longitude=Decimal("-58.4"),
        )
        setup.add(field)
        await setup.commit()
        return engine, session_factory, user.id, field.id


async def _ingest_and_hold_the_transaction(
    session_factory: async_sessionmaker[AsyncSession],
    provider: MockWeatherProvider,
    field_id: int,
    *,
    source: str,
    ingested: asyncio.Event,
    release: asyncio.Event,
) -> int:
    """Ingesta en su propia conexión, avisa que el INSERT corrió y espera para commitear."""
    async with session_factory() as session:
        written = await ingest_forecasts(
            session, provider, start=START, days=1, source=source, field_ids=[field_id]
        )
        ingested.set()
        await release.wait()
        await session.commit()
        return written


async def test_concurrent_older_first_then_newer_stores_the_newest_issue() -> None:
    """El lote viejo inserta primero y el nuevo commitea después: gana el más nuevo."""
    engine, session_factory, user_id, field_id = await _new_concurrent_setup()
    older_ingested, older_release = asyncio.Event(), asyncio.Event()
    newer_ingested, newer_release = asyncio.Event(), asyncio.Event()
    try:
        older_task = asyncio.create_task(
            _ingest_and_hold_the_transaction(
                session_factory,
                MockWeatherProvider(seed=1, now=EARLIER),
                field_id,
                source="older",
                ingested=older_ingested,
                release=older_release,
            )
        )
        await asyncio.wait_for(older_ingested.wait(), timeout=5)

        newer_task = asyncio.create_task(
            _ingest_and_hold_the_transaction(
                session_factory,
                MockWeatherProvider(seed=2, now=LATER),
                field_id,
                source="newer",
                ingested=newer_ingested,
                release=newer_release,
            )
        )
        # El upsert nuevo queda esperando el conflicto de la fila sin commitear del viejo.
        await asyncio.sleep(0.2)
        assert not newer_task.done()

        older_release.set()
        older_written = await asyncio.wait_for(older_task, timeout=10)
        await asyncio.wait_for(newer_ingested.wait(), timeout=5)
        newer_release.set()
        newer_written = await asyncio.wait_for(newer_task, timeout=10)

        assert (older_written, newer_written) == (len(EventType), len(EventType))
        async with session_factory() as check:
            rows = (
                (
                    await check.execute(
                        select(WeatherForecast).where(WeatherForecast.field_id == field_id)
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == len(EventType)
        assert {row.issued_at for row in rows} == {LATER}
        assert {row.source for row in rows} == {"newer"}
        expected = {
            (START, event_type): random.Random(
                f"2:{field_id}:{START.isoformat()}:{event_type.value}"
            ).randint(0, 100)
            for event_type in EventType
        }
        assert {(row.forecast_date, row.event_type): row.probability for row in rows} == expected
    finally:
        async with engine.begin() as conn:
            await conn.execute(delete(User).where(User.id == user_id))
        await engine.dispose()


async def test_concurrent_newer_first_then_older_keeps_the_newest_issue() -> None:
    """El lote viejo llega tarde (el nuevo ya commiteó): no lo pisa y no cuenta filas."""
    engine, session_factory, user_id, field_id = await _new_concurrent_setup()
    newer_ingested, newer_release = asyncio.Event(), asyncio.Event()
    older_ingested, older_release = asyncio.Event(), asyncio.Event()
    try:
        newer_task = asyncio.create_task(
            _ingest_and_hold_the_transaction(
                session_factory,
                MockWeatherProvider(seed=2, now=LATER),
                field_id,
                source="newer",
                ingested=newer_ingested,
                release=newer_release,
            )
        )
        await asyncio.wait_for(newer_ingested.wait(), timeout=5)

        older_task = asyncio.create_task(
            _ingest_and_hold_the_transaction(
                session_factory,
                MockWeatherProvider(seed=1, now=EARLIER),
                field_id,
                source="older",
                ingested=older_ingested,
                release=older_release,
            )
        )
        await asyncio.sleep(0.2)
        assert not older_task.done()

        newer_release.set()
        newer_written = await asyncio.wait_for(newer_task, timeout=10)
        await asyncio.wait_for(older_ingested.wait(), timeout=5)
        older_release.set()
        older_written = await asyncio.wait_for(older_task, timeout=10)

        assert (newer_written, older_written) == (len(EventType), 0)
        async with session_factory() as check:
            rows = (
                (
                    await check.execute(
                        select(WeatherForecast).where(WeatherForecast.field_id == field_id)
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == len(EventType)
        assert {row.issued_at for row in rows} == {LATER}
        assert {row.source for row in rows} == {"newer"}
    finally:
        async with engine.begin() as conn:
            await conn.execute(delete(User).where(User.id == user_id))
        await engine.dispose()


# --- Validación de `issued_at` antes del upsert (S11 SEC-1/SEC-2) ------------


class _IssuedAtProvider:
    """Provider con `issued_at` controlado por evento (incluye `None` y naive)."""

    def __init__(self, issued_by_event: dict[EventType, datetime | None]) -> None:
        self.issued_by_event = issued_by_event

    async def fetch(self, field: Field, *, start: date, days: int) -> list[ForecastPoint]:
        return [
            ForecastPoint(
                field_id=field.id,
                forecast_date=start + timedelta(days=offset),
                event_type=event_type,
                probability=50 + offset,
                issued_at=self.issued_by_event.get(event_type),
            )
            for offset in range(days)
            for event_type in EventType
        ]


async def test_an_issued_at_far_in_the_future_is_clamped_to_now_and_logged(
    db_session: AsyncSession,
    make_field: MakeField,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SEC-1: un dato de 2099 se guarda acotado a `now`, y una corrida posterior válida lo pisa."""
    field = await make_field()
    caplog.set_level(logging.WARNING, logger="app.weather.ingestion")
    provider = _IssuedAtProvider(dict.fromkeys(EventType, datetime(2099, 1, 1, tzinfo=UTC)))

    written = await ingest_forecasts(db_session, provider, start=START, days=1)

    assert written == len(EventType)
    rows = await _rows(db_session, field.id)
    assert {row.issued_at for row in rows} == {NOW}
    assert f"forecast_issued_in_future count={len(EventType)}" in caplog.text

    # Una corrida válida posterior (emitida después del `now` acotado) pisa el dato.
    later_run = NOW + timedelta(hours=1)
    written = await ingest_forecasts(
        db_session,
        MockWeatherProvider(seed=9, now=later_run),
        start=START,
        days=1,
        source="fresh",
        now=later_run,
    )

    assert written == len(EventType)
    rows = await _rows(db_session, field.id)
    assert {row.issued_at for row in rows} == {later_run}
    assert {row.source for row in rows} == {"fresh"}


async def test_an_issued_at_within_the_tolerance_is_stored_as_is(
    db_session: AsyncSession,
    make_field: MakeField,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SEC-1: dentro de `now + tolerancia` no se toca; es un desfasaje de reloj aceptable."""
    field = await make_field()
    caplog.set_level(logging.WARNING, logger="app.weather.ingestion")
    slightly_ahead = NOW + timedelta(minutes=30)
    provider = _IssuedAtProvider(dict.fromkeys(EventType, slightly_ahead))

    written = await ingest_forecasts(db_session, provider, start=START, days=1)

    assert written == len(EventType)
    assert {row.issued_at for row in await _rows(db_session, field.id)} == {slightly_ahead}
    assert "forecast_issued_in_future" not in caplog.text


async def test_invalid_issued_at_points_are_discarded_without_aborting_the_batch(
    db_session: AsyncSession,
    make_field: MakeField,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SEC-2: un `issued_at` `None` o naive descarta ese punto; el resto del lote se ingesta."""
    field = await make_field()
    caplog.set_level(logging.WARNING, logger="app.weather.ingestion")
    naive = datetime(2026, 11, 16, 12, 0)
    provider = _IssuedAtProvider(
        {
            EventType.FROST: None,
            EventType.RAIN: naive,
            EventType.HAIL: ISSUED_AT,
            EventType.STRONG_WIND: ISSUED_AT,
            EventType.HEAT_WAVE: ISSUED_AT,
        }
    )

    written = await ingest_forecasts(db_session, provider, start=START, days=1)

    assert written == len(EventType) - 2
    rows = await _rows(db_session, field.id)
    assert {row.event_type for row in rows} == {
        EventType.HAIL,
        EventType.STRONG_WIND,
        EventType.HEAT_WAVE,
    }
    assert {row.issued_at for row in rows} == {ISSUED_AT}
    assert "forecast_invalid_issued_at count=2" in caplog.text


async def test_an_issued_at_with_a_non_utc_offset_is_normalized_to_utc(
    db_session: AsyncSession,
    make_field: MakeField,
) -> None:
    """SEC-2: los aware se guardan en UTC (mismo instante, otra representación)."""
    field = await make_field()
    provider = _IssuedAtProvider(
        dict.fromkeys(EventType, datetime(2026, 11, 16, 9, 0, tzinfo=timezone(timedelta(hours=-3))))
    )

    await ingest_forecasts(db_session, provider, start=START, days=1)

    assert {row.issued_at for row in await _rows(db_session, field.id)} == {ISSUED_AT}
