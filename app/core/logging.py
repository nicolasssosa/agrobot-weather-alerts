"""Logging estructurado simple (key=value) a stdout."""

import logging

from app.core.config import get_settings


def configure_logging() -> None:
    logging.basicConfig(
        level=get_settings().log_level.upper(),
        format="%(asctime)s level=%(levelname)s logger=%(name)s %(message)s",
    )
