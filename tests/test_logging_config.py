from datetime import UTC, datetime
import logging

from usd1_monitor.logging_config import configure_logging


def test_logging_uses_configured_timezone(tmp_path) -> None:
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    try:
        configure_logging(
            tmp_path / "monitor.log", timezone_name="Asia/Shanghai"
        )
        record = logging.LogRecord(
            "usd1", logging.INFO, __file__, 1, "message", (), None
        )
        record.created = datetime(2026, 9, 7, 4, 0, tzinfo=UTC).timestamp()

        rendered = root.handlers[0].formatter.format(record)

        assert rendered.startswith("2026-09-07 12:00:00")
    finally:
        for handler in root.handlers:
            if handler not in original_handlers:
                handler.close()
        root.handlers[:] = original_handlers
        root.setLevel(original_level)
