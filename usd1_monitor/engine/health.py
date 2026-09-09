from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from usd1_monitor.models import RiskLevel


@dataclass(frozen=True)
class CollectorHealth:
    collector_id: str
    consecutive_failures: int
    last_success_at: datetime | None
    last_error: str | None
    last_failure_at: datetime | None = None
    first_failure_at: datetime | None = None


def evaluate_health(
    health: CollectorHealth,
    now: datetime,
    *,
    critical: bool,
    stale_seconds: int = 900,
) -> RiskLevel:
    stale_since = health.last_success_at or health.first_failure_at
    if critical and stale_since is not None and (
        now - stale_since
    ).total_seconds() >= stale_seconds:
        return RiskLevel.RED
    if health.consecutive_failures >= 3:
        return RiskLevel.YELLOW
    return RiskLevel.GREEN
