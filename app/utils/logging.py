"""Central logging utilities for BotYo."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

ROOT_DIR = Path(__file__).resolve().parents[2]
LOG_FILE_PATH = ROOT_DIR / "data" / "logs" / "botyo.log"

_CONFIG_LOCK = Lock()
_CONFIGURED = False


class UTCFormatter(logging.Formatter):
    """Formatter that emits UTC timestamps for all log records."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=timezone.utc)
        if datefmt:
            return timestamp.strftime(datefmt)
        return timestamp.strftime("%Y-%m-%d %H:%M:%S")


def _configure_logging() -> None:
    global _CONFIGURED

    if _CONFIGURED:
        return

    with _CONFIG_LOCK:
        if _CONFIGURED:
            return

        LOG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)

        formatter = UTCFormatter("[%(asctime)s UTC] [%(levelname)s] [%(name)s] %(message)s")
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)

        file_handler = logging.FileHandler(LOG_FILE_PATH, encoding="utf-8")
        file_handler.setFormatter(formatter)

        root_logger = logging.getLogger()
        root_logger.handlers.clear()
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(console_handler)
        root_logger.addHandler(file_handler)

        _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a logger configured with BotYo defaults."""

    _configure_logging()
    return logging.getLogger(name)

