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


# --- Límites operativos (S14a, D16) -----------------------------------------


def test_db_timeouts_default_to_30s_and_5s() -> None:
    """`DB_STATEMENT_TIMEOUT_MS` = 30000 y `DB_LOCK_TIMEOUT_MS` = 5000 (docs/BACKLOG.md §S14a)."""
    settings = Settings()
    assert settings.db_statement_timeout_ms == 30000
    assert settings.db_lock_timeout_ms == 5000


@pytest.mark.parametrize(
    ("field", "value"),
    [("db_statement_timeout_ms", 0), ("db_lock_timeout_ms", 0)],
)
def test_db_timeouts_accept_zero_as_no_limit(field: str, value: int) -> None:
    """`0` = sin límite (valor de Postgres para ambos GUC)."""
    assert getattr(Settings(**{field: value}), field) == value


@pytest.mark.parametrize("field", ["db_statement_timeout_ms", "db_lock_timeout_ms"])
@pytest.mark.parametrize("value", [-1, "abc", "1.5"])
def test_db_timeouts_reject_negative_or_non_integer_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(**{field: value})


def test_db_timeouts_read_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_STATEMENT_TIMEOUT_MS", "1000")
    monkeypatch.setenv("DB_LOCK_TIMEOUT_MS", "250")
    settings = Settings()
    assert (settings.db_statement_timeout_ms, settings.db_lock_timeout_ms) == (1000, 250)


def test_max_fields_per_user_defaults_to_20() -> None:
    """Cuota de campos por usuario (docs/API.md §fields, S14a)."""
    assert Settings().max_fields_per_user == 20


@pytest.mark.parametrize("value", [1, 20, 1000])
def test_max_fields_per_user_accepts_the_documented_range(value: int) -> None:
    assert Settings(max_fields_per_user=value).max_fields_per_user == value


@pytest.mark.parametrize("value", [0, -1, 1001, "abc", "20.5"])
def test_max_fields_per_user_rejects_out_of_range_values(value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(max_fields_per_user=value)  # type: ignore[arg-type]


def test_max_request_body_bytes_defaults_to_64_kib() -> None:
    """Tamaño máximo del body (docs/API.md §Convenciones, S14a)."""
    assert Settings().max_request_body_bytes == 65536


@pytest.mark.parametrize("value", [1024, 65536, 1048576])
def test_max_request_body_bytes_accepts_the_documented_range(value: int) -> None:
    assert Settings(max_request_body_bytes=value).max_request_body_bytes == value


@pytest.mark.parametrize("value", [0, 1023, 1048577, "abc", "65536.5"])
def test_max_request_body_bytes_rejects_out_of_range_values(value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(max_request_body_bytes=value)  # type: ignore[arg-type]
