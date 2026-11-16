"""Tests del script `python -m app.scripts.ingest`. Ver docs/JOBS.md §Ingesta mock.

El parseo de `--days` y el cableado de `main` no tocan la base; el test de dos corridas
seguidas usa la base de test (`TEST_DATABASE_URL`), vacía y con el esquema migrado.
"""

import argparse

import pytest

from app.core import db as db_module
from app.core.config import Settings
from app.scripts import ingest as ingest_module
from app.scripts.ingest import _DEFAULT_DAYS, _MAX_DAYS, _MIN_DAYS, _parse_days, main
from tests.conftest import TEST_DATABASE_URL


@pytest.mark.parametrize("value", ["1", str(_DEFAULT_DAYS), str(_MAX_DAYS)])
def test_parse_days_accepts_the_documented_range(value: str) -> None:
    assert _parse_days(value) == int(value)


@pytest.mark.parametrize(
    "value",
    ["0", "31", "-1", "100", "", "abc", "1.5"],
)
def test_parse_days_rejects_invalid_values(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_days(value)


@pytest.mark.parametrize(
    "argv",
    [
        ["--days", "0"],
        ["--days", str(_MAX_DAYS + 1)],
        ["--days", "-1"],
        ["--days", "abc"],
        ["--days", "abc" + str(_MIN_DAYS)],
    ],
)
def test_main_rejects_invalid_days_with_exit_code_2(argv: list[str]) -> None:
    # argparse corta antes de `asyncio.run(_run(...))`, así que no se toca la base.
    with pytest.raises(SystemExit) as exit_info:
        main(argv)

    assert exit_info.value.code == 2


def test_main_runs_the_ingestion_with_the_default_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    async def _fake_run(days: int) -> int:
        calls.append(days)
        return 0

    monkeypatch.setattr(ingest_module, "_run", _fake_run)

    main([])

    assert calls == [_DEFAULT_DAYS]


def test_main_forwards_the_days_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    async def _fake_run(days: int) -> int:
        calls.append(days)
        return 0

    monkeypatch.setattr(ingest_module, "_run", _fake_run)

    main(["--days", "7"])

    assert calls == [7]


def test_main_can_run_twice_in_the_same_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """CR-1: cada corrida descarta su engine y lo saca del `lru_cache`."""
    settings = Settings(database_url=TEST_DATABASE_URL)
    monkeypatch.setattr(db_module, "get_settings", lambda: settings)
    db_module.get_engine.cache_clear()
    db_module.get_sessionmaker.cache_clear()
    try:
        main([])
        after_first = db_module.get_engine()
        main([])
        after_second = db_module.get_engine()
    finally:
        db_module.get_engine.cache_clear()
        db_module.get_sessionmaker.cache_clear()

    # Sin `cache_clear()` la 2da corrida reusaría el engine disposed de la 1ra (mismo objeto).
    assert after_first is not after_second
