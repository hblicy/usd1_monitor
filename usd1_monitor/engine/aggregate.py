from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from usd1_monitor.models import RiskLevel, RiskState


def is_monitoring_health_rule(rule_id: str) -> bool:
    return rule_id.startswith("health.") or rule_id == "por.age"


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
        if not is_monitoring_health_rule(state.rule_id)
        and (
            not state.rule_id.startswith("event.")
            or state.changed_at > event_cutoff
        )
    ]
    return max(current, default=RiskLevel.GREEN)


def health_overall(states: Iterable[RiskState]) -> RiskLevel:
    current = [
        state.level
        for state in states
        if is_monitoring_health_rule(state.rule_id)
    ]
    return max(current, default=RiskLevel.GREEN)
