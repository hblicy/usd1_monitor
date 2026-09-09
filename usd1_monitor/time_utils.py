from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo


def display_timezone(name: str) -> tzinfo:
    if name == "UTC":
        return timezone.utc
    if name == "Asia/Shanghai":
        return timezone(timedelta(hours=8), name)
    return ZoneInfo(name)


def local_iso(value: datetime | str, timezone_name: str = "Asia/Shanghai") -> str:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        raise ValueError("display time must be timezone-aware")
    return parsed.astimezone(display_timezone(timezone_name)).isoformat()
