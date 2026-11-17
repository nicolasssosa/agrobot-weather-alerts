"""Corre una ingesta mock: `python -m app.scripts.ingest [--days 14]`.

Usa `MockWeatherProvider` + `ingest_forecasts(start=hoy UTC)` y commitea al final.
Spec: docs/JOBS.md §Ingesta mock.
"""

import argparse
import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.core.db import get_engine, get_sessionmaker
from app.core.logging import configure_logging
from app.models import Field
from app.weather.ingestion import ingest_forecasts
from app.weather.provider import MockWeatherProvider

logger = logging.getLogger(__name__)

_DEFAULT_DAYS = 14
_MIN_DAYS = 1
_MAX_DAYS = 30


def _parse_days(value: str) -> int:
    """Valida `--days`: entero entre 1 y 30."""
    try:
        days = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("days must be an integer") from None
    if not _MIN_DAYS <= days <= _MAX_DAYS:
        raise argparse.ArgumentTypeError(f"days must be between {_MIN_DAYS} and {_MAX_DAYS}")
    return days


async def _run(days: int) -> int:
    """Ingesta desde hoy (UTC), commitea y loguea el resultado."""
    now = datetime.now(UTC)
    start = now.date()
    try:
        async with get_sessionmaker()() as session:
            field_count = await session.scalar(select(func.count()).select_from(Field))
            rows = await ingest_forecasts(
                session, MockWeatherProvider(now=now), start=start, days=days, now=now
            )
            await session.commit()
        logger.info("forecasts_ingested rows=%d fields=%d", rows, field_count or 0)
        return rows
    finally:
        await get_engine().dispose()
        # `lru_cache`: sin limpiar, un segundo `main()` en el mismo proceso reusaría un engine
        # disposed (su pool quedó atado a un event loop ya cerrado).
        get_engine.cache_clear()
        get_sessionmaker.cache_clear()


def main(argv: Sequence[str] | None = None) -> None:
    """Punto de entrada del script."""
    configure_logging()
    parser = argparse.ArgumentParser(description="Ingesta mock de pronósticos")
    parser.add_argument(
        "--days",
        type=_parse_days,
        default=_DEFAULT_DAYS,
        help=(
            f"días a pronosticar desde hoy UTC ({_MIN_DAYS}..{_MAX_DAYS}, default {_DEFAULT_DAYS})"
        ),
    )
    args = parser.parse_args(argv)
    asyncio.run(_run(args.days))


if __name__ == "__main__":
    main()
