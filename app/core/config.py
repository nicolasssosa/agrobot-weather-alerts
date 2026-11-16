"""Configuración de la aplicación (12-factor, vía variables de entorno / .env)."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, PositiveInt
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: Literal["local", "test", "staging", "production"] = "local"
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/agrobot"
    db_echo: bool = False
    db_pool_size: PositiveInt = 5
    db_max_overflow: int = Field(default=10, ge=0)

    # Background job
    worker_interval_seconds: PositiveInt = 60
    dispatch_batch_size: int = Field(default=100, ge=1, le=1000)
    dispatch_max_attempts: int = Field(default=5, ge=1, le=20)
    dispatch_backoff_base_seconds: PositiveInt = 30
    notifier_timeout_seconds: float = Field(default=10.0, gt=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()
