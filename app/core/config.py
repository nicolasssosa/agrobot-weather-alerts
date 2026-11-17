"""Configuración de la aplicación (12-factor, vía variables de entorno / .env)."""

from functools import lru_cache
from math import ceil
from typing import Literal, Self

from pydantic import Field, PositiveInt, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: Literal["local", "test", "staging", "production"] = "local"
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/agrobot"
    db_echo: bool = False
    db_pool_size: PositiveInt = 5
    db_max_overflow: int = Field(default=10, ge=0)

    # Tope de alertas vivas por campo (docs/API.md §alerts, "Límite").
    max_alerts_per_field: int = Field(default=10, ge=1, le=100)

    # Frescura máxima de un pronóstico para evaluarlo (docs/JOBS.md §Evaluador, D14).
    max_forecast_age_hours: int = Field(default=24, ge=1, le=168)

    # Horizonte de evaluación en días desde el "hoy" local de cada campo (D2, S12a): 0 = sin límite.
    alert_horizon_days: int = Field(default=7, ge=0, le=365)

    # Tolerancia de reloj para un `issued_at` en el futuro (docs/JOBS.md §Ingesta mock, D14).
    forecast_future_tolerance_minutes: int = Field(default=60, ge=0, le=1440)

    # Vigencia de las respuestas guardadas con `Idempotency-Key` (docs/API.md).
    idempotency_ttl_hours: int = Field(default=24, ge=1, le=168)

    # Background job
    worker_interval_seconds: PositiveInt = 60
    dispatch_batch_size: int = Field(default=100, ge=1, le=1000)
    dispatch_max_attempts: int = Field(default=5, ge=1, le=20)
    dispatch_backoff_base_seconds: PositiveInt = 30
    notifier_timeout_seconds: float = Field(default=10.0, gt=0)
    dispatch_concurrency: int = Field(default=10, ge=1, le=100)
    dispatch_lease_seconds: int = Field(default=120, ge=10, le=3600)

    @model_validator(mode="after")
    def _lease_covers_the_worst_case_batch(self) -> Self:
        """El lease debe superar el peor caso de envío del lote (docs/JOBS.md §Dispatcher).

        Si no, otro worker reclamaría notificaciones que todavía se están enviando.
        """
        worst_case_seconds = (
            ceil(self.dispatch_batch_size / self.dispatch_concurrency)
            * self.notifier_timeout_seconds
        )
        if self.dispatch_lease_seconds <= worst_case_seconds:
            raise ValueError(
                "dispatch_lease_seconds "
                f"({self.dispatch_lease_seconds}) must be greater than the worst-case batch "
                f"time of ceil({self.dispatch_batch_size} / {self.dispatch_concurrency}) * "
                f"{self.notifier_timeout_seconds} = {worst_case_seconds:g} seconds"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
