from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from usd1_monitor.models import RiskLevel, RiskState


def business_overall(
    states: Iterable[RiskState],
    *,
    now: datetime | None = None,
    event_active_seconds: int = 3600,
) -> RiskLevel:
    current_time = now or datetime.now(UTC)
    event_cutoff = current_time - timedelta(seconds=event_active_seconds)
    current = [
        state.level
        for state in states
        if not state.rule_id.startswith("health.")
        and (
            not state.rule_id.startswith("event.")
            or state.changed_at > event_cutoff
        )
    ]
    return max(current, default=RiskLevel.GREEN)


def health_overall(states: Iterable[RiskState]) -> RiskLevel:
    current = [
        state.level for state in states if state.rule_id.startswith("health.")
    ]
    return max(current, default=RiskLevel.GREEN)
