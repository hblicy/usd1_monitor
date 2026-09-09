from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
from pathlib import Path

from usd1_monitor.time_utils import display_timezone


def configure_logging(
    path: Path,
    *,
    max_bytes: int = 10_000_000,
    backup_count: int = 5,
    timezone_name: str = "Asia/Shanghai",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    timezone = display_timezone(timezone_name)
    formatter.converter = lambda timestamp: datetime.fromtimestamp(
        timestamp, timezone
    ).timetuple()
    file_handler = RotatingFileHandler(
        path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(stream_handler)
