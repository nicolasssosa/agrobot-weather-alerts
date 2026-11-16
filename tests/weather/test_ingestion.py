"""Tests de la ingesta mock. Ver docs/TESTING.md §test_ingestion y docs/JOBS.md §Ingesta mock."""

import random
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EventType, Field, WeatherForecast
from app.weather import ingestion as ingestion_module
from app.weather.ingestion import ingest_forecasts
from app.weather.provider import ForecastPoint, MockWeatherProvider
from tests.conftest import MakeField, MakeForecast, MakeUser

START = date(2026, 11, 16)


async def _rows(session: AsyncSession, field_id: int) -> list[WeatherForecast]:
    result = await session.execute(
        select(WeatherForecast)
        .where(WeatherForecast.field_id == field_id)
        .order_by(WeatherForecast.forecast_date, WeatherForecast.event_type)
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
        db_session, MockWeatherProvider(seed=1), start=START, days=2, source="mock"
    )
    second = await ingest_forecasts(
        db_session, MockWeatherProvider(seed=2), start=START, days=2, source="fresh"
    )

    assert first == second == 2 * len(EventType)
    rows = await _rows(db_session, field.id)
    assert len(rows) == 2 * len(EventType)
    assert all(row.source == "fresh" for row in rows)
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
        fetched_at=old,
    )
    existing.created_at = old
    existing.updated_at = old
    db_session.add(existing)
    await db_session.flush()

    await ingest_forecasts(db_session, MockWeatherProvider(seed=3), start=START, days=1)

    await db_session.refresh(existing)
    assert existing.probability == random.Random(
        f"3:{field.id}:{START.isoformat()}:{EventType.FROST.value}"
    ).randint(0, 100)
    assert existing.source == "mock"
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
    provider = MockWeatherProvider(seed=11)
    # Cada fila bindea 6 columnas: con `_MAX_PARAMS = 12` cada statement lleva 2 filas.
    monkeypatch.setattr(ingestion_module, "_MAX_PARAMS", 12)
    original = ingestion_module._upsert_chunk
    chunks: list[int] = []

    async def _spy(session: AsyncSession, rows: Sequence[dict[str, object]]) -> None:
        chunks.append(len(rows))
        await original(session, rows)

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
