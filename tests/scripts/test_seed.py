"""Tests del seed demo (docs/JOBS.md §Seed).

Corre el seed dos veces sobre el `sessionmaker` del test: el rollback final limpia todo, así
que las cantidades absolutas son las del seed y no las de la base compartida.
"""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.pii import mask_phone
from app.jobs.worker import run_cycle
from app.models import (
    Alert,
    EventType,
    Field,
    Notification,
    NotificationStatus,
    User,
    WeatherForecast,
)
from app.scripts.seed import (
    SeedAlert,
    SeedField,
    SeedSummary,
    SeedUser,
    _print_summary,
    main,
    seed,
)
from tests.conftest import FakeNotifier

NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
#: Hora de claim del worker, posterior a cualquier `now()` real del test y dentro de la
#: ventana de frescura del pronóstico emitido en `NOW` (S11).
CLAIM_NOW = datetime(2030, 1, 1, 12, 1, tzinfo=UTC)
EXPECTED_FORECASTS = 14 * 5 * 3


@pytest.fixture(autouse=True)
def _explicit_demo_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Los tests de este módulo corren como una demo con `ENVIRONMENT` explícita (S7 SEC-5).

    Los casos de la guarda la pisan o la quitan según lo que prueban.
    """
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setattr("app.scripts.seed.get_settings", lambda: Settings(_env_file=None))


async def test_seed_then_one_worker_cycle_sends_notifications(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    fake_notifier: FakeNotifier,
) -> None:
    """Equivalente automatizado de `make seed jobs-once`: todo lo disparado queda `sent`."""
    await seed(test_sessionmaker, now=NOW)

    result = await run_cycle(
        test_sessionmaker,
        fake_notifier,
        Settings(
            dispatch_batch_size=100,
            dispatch_max_attempts=3,
            dispatch_backoff_base_seconds=30,
            notifier_timeout_seconds=1.0,
            dispatch_concurrency=10,
            dispatch_lease_seconds=60,
        ),
        now=NOW,
        clock=lambda: CLAIM_NOW,
    )

    assert result.created > 0
    assert result.dispatch.sent == result.created
    assert result.dispatch.retried == 0
    assert result.dispatch.failed == 0
    assert len(fake_notifier.sent) == result.created
    statuses = (await db_session.scalars(select(Notification.status))).all()
    assert len(statuses) == result.created
    assert all(status == NotificationStatus.SENT for status in statuses)


async def test_seed_is_idempotent_and_creates_the_documented_data(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Correrlo dos veces deja exactamente 2 usuarios, 3 campos, 4 alertas y 210 forecasts."""
    first = await seed(test_sessionmaker, now=NOW)

    users = (await db_session.scalars(select(User).order_by(User.id))).all()
    fields = (await db_session.scalars(select(Field).order_by(Field.id))).all()
    alert_rows = (
        await db_session.execute(select(Alert, Field.name).join(Field, Field.id == Alert.field_id))
    ).all()
    forecasts = await db_session.scalar(select(func.count()).select_from(WeatherForecast))

    assert [user.phone_number for user in users] == ["+5491100000001", "+5491100000002"]
    assert [user.name for user in users] == ["Ana Gómez", "Bruno Díaz"]
    assert [field.name for field in fields] == ["Lote Norte", "Lote Sur", "La Esperanza"]
    assert {(alert.event_type, alert.threshold, name) for alert, name in alert_rows} == {
        (EventType.FROST, 60, "Lote Norte"),
        (EventType.RAIN, 80, "Lote Norte"),
        (EventType.HAIL, 50, "Lote Sur"),
        (EventType.HEAT_WAVE, 40, "La Esperanza"),
    }
    assert forecasts == EXPECTED_FORECASTS == first.forecast_rows

    lote_norte = next(field for field in fields if field.name == "Lote Norte")
    la_esperanza = next(field for field in fields if field.name == "La Esperanza")
    assert lote_norte.latitude == Decimal("-34.60")
    assert lote_norte.longitude == Decimal("-58.38")
    assert la_esperanza.latitude == Decimal("-31.42")
    assert la_esperanza.longitude == Decimal("-64.18")

    # Segunda corrida: mismas entidades y cantidades, sin duplicados ni errores. La ingesta
    # no escribe filas porque la corrida del seed tiene el mismo `issued_at` (S11).
    second = await seed(test_sessionmaker, now=NOW)

    assert second.forecast_rows == 0
    assert (second.users, second.fields, second.alerts) == (
        first.users,
        first.fields,
        first.alerts,
    )
    assert len((await db_session.scalars(select(User))).all()) == 2
    assert len((await db_session.scalars(select(Field))).all()) == 3
    assert len((await db_session.scalars(select(Alert))).all()) == 4
    forecasts_after = await db_session.scalar(select(func.count()).select_from(WeatherForecast))
    assert forecasts_after == EXPECTED_FORECASTS


@pytest.mark.parametrize("environment", ["staging", "production"])
async def test_seed_aborts_in_unsafe_environments_without_touching_the_database(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
) -> None:
    """En staging/production el seed sale con error antes de crear nada (S7 SEC-1)."""
    monkeypatch.setattr("app.scripts.seed.get_settings", lambda: Settings(environment=environment))

    with pytest.raises(SystemExit, match=environment):
        await seed(test_sessionmaker, now=NOW)

    assert await db_session.scalar(select(func.count()).select_from(User)) == 0
    assert await db_session.scalar(select(func.count()).select_from(WeatherForecast)) == 0


async def test_seed_requires_an_explicit_environment(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S7 SEC-5: sin `ENVIRONMENT` (ni `.env`) y sin `--yes-demo` aborta antes de tocar la base."""
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.setattr("app.scripts.seed.get_settings", lambda: Settings(_env_file=None))

    with pytest.raises(SystemExit) as excinfo:
        await seed(test_sessionmaker, now=NOW)

    message = str(excinfo.value)
    assert "ENVIRONMENT" in message
    assert "--yes-demo" in message
    assert await db_session.scalar(select(func.count()).select_from(User)) == 0
    assert await db_session.scalar(select(func.count()).select_from(WeatherForecast)) == 0


async def test_seed_runs_with_an_explicit_environment(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S7 SEC-5: con `ENVIRONMENT` definida explícitamente el seed corre sin el flag."""
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setattr("app.scripts.seed.get_settings", lambda: Settings(_env_file=None))

    summary = await seed(test_sessionmaker, now=NOW)

    assert summary.forecast_rows == EXPECTED_FORECASTS
    assert len((await db_session.scalars(select(User))).all()) == 2


async def test_seed_runs_with_yes_demo_without_an_environment(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S7 SEC-5: `--yes-demo` (acá `yes_demo=True`) es la salida explícita para una demo."""
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    settings = Settings(_env_file=None)
    assert "environment" not in settings.model_fields_set
    monkeypatch.setattr("app.scripts.seed.get_settings", lambda: settings)

    summary = await seed(test_sessionmaker, now=NOW, yes_demo=True)

    assert summary.forecast_rows == EXPECTED_FORECASTS
    assert len((await db_session.scalars(select(Alert))).all()) == 4


async def test_seed_counts_an_environment_from_the_env_file_as_explicit(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """S7 SEC-5: `ENVIRONMENT` en `.env` es explícita; no hace falta exportarla ni el flag."""
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("ENVIRONMENT=test\n", encoding="utf-8")
    settings = Settings(_env_file=env_file)
    assert "environment" in settings.model_fields_set
    monkeypatch.setattr("app.scripts.seed.get_settings", lambda: settings)

    summary = await seed(test_sessionmaker, now=NOW)

    assert summary.forecast_rows == EXPECTED_FORECASTS
    assert len((await db_session.scalars(select(User))).all()) == 2


@pytest.mark.parametrize("environment", ["staging", "production"])
async def test_yes_demo_does_not_bypass_the_unsafe_environment_guard(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
) -> None:
    """S7 SEC-5: `--yes-demo` habilita la demo, no la denylist de staging/production."""
    monkeypatch.setattr("app.scripts.seed.get_settings", lambda: Settings(environment=environment))

    with pytest.raises(SystemExit, match=environment):
        await seed(test_sessionmaker, now=NOW, yes_demo=True)

    assert await db_session.scalar(select(func.count()).select_from(User)) == 0
    assert await db_session.scalar(select(func.count()).select_from(WeatherForecast)) == 0


def test_main_forwards_yes_demo_to_the_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    """S7 SEC-5: el CLI `--yes-demo` llega al seed; sin el flag queda en `False`."""
    captured: list[bool] = []

    async def fake_run(now: datetime | None = None, *, yes_demo: bool = False) -> SeedSummary:
        captured.append(yes_demo)
        return SeedSummary(users=(), fields=(), alerts=(), forecast_rows=0)

    monkeypatch.setattr("app.scripts.seed._run", fake_run)
    monkeypatch.setattr("app.scripts.seed._print_summary", lambda summary: None)
    monkeypatch.setattr("app.scripts.seed.configure_logging", lambda: None)

    main(["--yes-demo"])
    main([])

    assert captured == [True, False]


def test_main_without_environment_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    """S7 SEC-5: el CLI aborta con exit code ≠ 0 si `ENVIRONMENT` no está definida."""
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.setattr("app.scripts.seed.get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr("app.scripts.seed.configure_logging", lambda: None)

    with pytest.raises(SystemExit) as excinfo:
        main([])

    assert excinfo.value.code not in (0, None)
    assert "ENVIRONMENT" in str(excinfo.value)


async def test_seed_does_not_touch_forecasts_of_foreign_fields(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    make_field,  # type: ignore[no-untyped-def]
    make_forecast,  # type: ignore[no-untyped-def]
) -> None:
    """La ingesta del seed se limita a sus 3 campos: un campo ajeno queda intacto (S7 SEC-1)."""
    foreign = await make_field(name="Campo Ajeno")
    existing = await make_forecast(
        foreign,
        forecast_date=NOW.date(),
        event_type=EventType.RAIN,
        probability=7,
        source="manual",
    )

    await seed(test_sessionmaker, now=NOW)

    await db_session.refresh(existing)
    assert existing.probability == 7
    assert existing.source == "manual"
    foreign_rows = await db_session.scalar(
        select(func.count())
        .select_from(WeatherForecast)
        .where(WeatherForecast.field_id == foreign.id)
    )
    assert foreign_rows == 1


async def test_print_summary_shows_ids_and_masks_the_phone(capsys) -> None:  # type: ignore[no-untyped-def]
    """El resumen imprime ids, filas y un curl, pero nunca el teléfono completo."""
    summary = SeedSummary(
        users=(SeedUser(id=7, name="Ana Gómez", phone_number="+5491100000001"),),
        fields=(SeedField(id=9, user_id=7, name="Lote Norte"),),
        alerts=(SeedAlert(id=11, field_id=9, event_type=EventType.FROST, threshold=60),),
        forecast_rows=210,
    )

    _print_summary(summary)

    output = capsys.readouterr().out
    assert "+5491100000001" not in output
    assert "***0001" in output
    assert "X-User-Id: 7" in output
    assert "forecasts=210" in output
    assert "alert id=11" in output


async def test_print_summary_of_a_real_seed_never_exposes_a_full_phone(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    capsys,  # type: ignore[no-untyped-def]
) -> None:
    """Ninguno de los teléfonos reales del seed sale en claro; los ids y el conteo sí."""
    summary = await seed(test_sessionmaker, now=NOW)

    _print_summary(summary)

    output = capsys.readouterr().out
    for user in summary.users:
        assert user.phone_number not in output
        assert mask_phone(user.phone_number) in output
        assert f"user id={user.id}" in output
    for field in summary.fields:
        assert f"field id={field.id}" in output
    assert f"forecasts={EXPECTED_FORECASTS}" in output


async def test_seed_curl_example_uses_the_api_prefix_and_works(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    client,  # type: ignore[no-untyped-def]
    capsys,  # type: ignore[no-untyped-def]
) -> None:
    """El ejemplo impreso apunta a `/api/v1/...` y, ejecutado, devuelve 200."""
    summary = await seed(test_sessionmaker, now=NOW)

    _print_summary(summary)

    output = capsys.readouterr().out
    curl_line = next(line for line in output.splitlines() if "curl" in line)
    url = curl_line.rsplit(" ", 1)[-1]
    field_id = summary.fields[0].id
    user_id = summary.users[0].id

    assert url == f"http://localhost:8000/api/v1/fields/{field_id}/forecasts"

    path = url.removeprefix("http://localhost:8000")
    response = await client.get(path, headers={"X-User-Id": str(user_id)})

    assert response.status_code == 200
    assert response.json()


async def test_seed_reuses_an_existing_user_with_another_name(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
    make_user,  # type: ignore[no-untyped-def]
) -> None:
    """Un usuario ya presente con el mismo teléfono no se duplica ni rompe el seed."""
    existing = await make_user(phone_number="+5491100000001", name="Ana Previa")

    summary = await seed(test_sessionmaker, now=NOW)

    users = (await db_session.scalars(select(User).order_by(User.id))).all()
    assert [user.phone_number for user in users] == ["+5491100000001", "+5491100000002"]
    ana = next(user for user in users if user.phone_number == "+5491100000001")
    assert ana.id == existing.id
    assert len(summary.users) == 2
    assert {user.id for user in summary.users} == {user.id for user in users}


async def test_seed_stamps_the_model_run_on_every_forecast(
    db_session: AsyncSession,
    test_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """La ingesta del seed usa su `now` como corrida del modelo, no el reloj (S11)."""
    summary = await seed(test_sessionmaker, now=NOW)

    issued = set((await db_session.scalars(select(WeatherForecast.issued_at))).all())

    assert summary.forecast_rows == EXPECTED_FORECASTS
    assert issued == {NOW}


async def test_seed_forwards_the_configured_future_tolerance(
    test_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SEC-3: el seed pasa la tolerancia del setting a la ingesta, no el default del código."""
    captured: dict[str, object] = {}

    async def _fake_ingest(session: object, provider: object, **kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr("app.scripts.seed.ingest_forecasts", _fake_ingest)
    monkeypatch.setattr(
        "app.scripts.seed.get_settings",
        lambda: Settings(environment="local", forecast_future_tolerance_minutes=23),
    )

    summary = await seed(test_sessionmaker, now=NOW)

    assert summary.forecast_rows == 0
    assert captured["future_tolerance_minutes"] == 23
