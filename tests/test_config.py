"""Validación de `Settings` para la frescura de pronósticos.

Ver docs/JOBS.md §Evaluador (regla 7) y docs/REQUIREMENTS.md D14: la ventana
`MAX_FORECAST_AGE_HOURS` tiene default 24 y rango válido 1..168.
"""

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_max_forecast_age_hours_defaults_to_24() -> None:
    assert Settings().max_forecast_age_hours == 24


@pytest.mark.parametrize("value", [1, 24, 168])
def test_max_forecast_age_hours_accepts_the_documented_range(value: int) -> None:
    """Los bordes 1 y 168 son válidos (inclusive)."""
    assert Settings(max_forecast_age_hours=value).max_forecast_age_hours == value


@pytest.mark.parametrize("value", [0, -1, 169, 1000, "abc", "24.5"])
def test_max_forecast_age_hours_rejects_out_of_range_values(value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(max_forecast_age_hours=value)  # type: ignore[arg-type]


def test_max_forecast_age_hours_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_FORECAST_AGE_HOURS", "6")
    assert Settings().max_forecast_age_hours == 6


@pytest.mark.parametrize("value", ["0", "169", "not-a-number"])
def test_max_forecast_age_hours_from_the_environment_is_validated(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("MAX_FORECAST_AGE_HOURS", value)
    with pytest.raises(ValidationError):
        Settings()


def test_forecast_future_tolerance_defaults_to_60_minutes() -> None:
    assert Settings().forecast_future_tolerance_minutes == 60


@pytest.mark.parametrize("value", [0, 60, 1440])
def test_forecast_future_tolerance_accepts_the_documented_range(value: int) -> None:
    settings = Settings(forecast_future_tolerance_minutes=value)
    assert settings.forecast_future_tolerance_minutes == value


@pytest.mark.parametrize("value", [-1, 1441, "abc"])
def test_forecast_future_tolerance_rejects_out_of_range_values(value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(forecast_future_tolerance_minutes=value)  # type: ignore[arg-type]
