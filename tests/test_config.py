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


def test_alert_horizon_days_defaults_to_7() -> None:
    """D2: horizonte de evaluación por defecto de 7 días (S12a)."""
    assert Settings().alert_horizon_days == 7


@pytest.mark.parametrize("value", [0, 1, 7, 365])
def test_alert_horizon_days_accepts_the_documented_range(value: int) -> None:
    """`0` = sin límite y los bordes 1..365 son válidos (inclusive)."""
    assert Settings(alert_horizon_days=value).alert_horizon_days == value


@pytest.mark.parametrize("value", [-1, 366, 1000, "abc"])
def test_alert_horizon_days_rejects_out_of_range_values(value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(alert_horizon_days=value)  # type: ignore[arg-type]


def test_alert_horizon_days_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALERT_HORIZON_DAYS", "0")
    assert Settings().alert_horizon_days == 0


def test_rearm_margin_defaults_to_10() -> None:
    """D1: margen de histéresis por defecto de 10 puntos (S12b)."""
    assert Settings().rearm_margin == 10


@pytest.mark.parametrize("value", [0, 1, 10, 98, 99])
def test_rearm_margin_accepts_the_documented_range(value: int) -> None:
    """0..99 inclusive: 0 deja la banda vacía y 99 es el máximo (siempre queda nivel 1)."""
    assert Settings(rearm_margin=value).rearm_margin == value


@pytest.mark.parametrize("value", [-1, 100, 1000, "abc", "10.5"])
def test_rearm_margin_rejects_out_of_range_values(value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(rearm_margin=value)  # type: ignore[arg-type]


def test_rearm_margin_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REARM_MARGIN", "25")
    assert Settings().rearm_margin == 25


def test_digest_max_items_defaults_to_10() -> None:
    """D15: tope de notificaciones por mensaje en el envío agrupado (S13)."""
    assert Settings().digest_max_items == 10


@pytest.mark.parametrize("value", [1, 10, 20])
def test_digest_max_items_accepts_the_documented_range(value: int) -> None:
    assert Settings(digest_max_items=value).digest_max_items == value


@pytest.mark.parametrize("value", [0, -1, 21, 1000, "abc", "10.5"])
def test_digest_max_items_rejects_out_of_range_values(value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(digest_max_items=value)  # type: ignore[arg-type]


def test_digest_max_items_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIGEST_MAX_ITEMS", "5")
    assert Settings().digest_max_items == 5
