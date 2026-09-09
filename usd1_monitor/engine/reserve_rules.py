from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from usd1_monitor.models import CoverageState, RiskLevel


@dataclass(frozen=True)
class PorReading:
    reserves: float
    observed_at: datetime
    collected_at: datetime | None = None


@dataclass(frozen=True)
class PorEvaluation:
    age_level: RiskLevel | CoverageState
    change_level: RiskLevel | CoverageState
    valid_recovery: bool


def evaluate_por_age(
    oracle_time: datetime,
    now: datetime,
    *,
    yellow_seconds: int = 3600,
    red_seconds: int = 7200,
) -> RiskLevel:
    age = (now - oracle_time).total_seconds()
    if age > red_seconds:
        return RiskLevel.RED
    if age > yellow_seconds:
        return RiskLevel.YELLOW
    return RiskLevel.GREEN


def evaluate_reserve_change(
    previous: float,
    current: float,
    *,
    threshold: float = 0.005,
) -> RiskLevel:
    if previous <= 0 or current <= 0:
        raise ValueError("reserve readings must be positive")
    return (
        RiskLevel.YELLOW
        if abs(current - previous) / previous > threshold
        else RiskLevel.GREEN
    )


def evaluate_por(
    readings: list[PorReading],
    now: datetime,
    *,
    yellow_seconds: int = 3600,
    red_seconds: int = 7200,
    change_threshold: float = 0.005,
    recovery_read_count: int = 2,
) -> PorEvaluation:
    if not readings:
        return PorEvaluation(
            CoverageState.UNKNOWN, CoverageState.UNKNOWN, False
        )
    distinct = {item.observed_at: item for item in readings}
    ordered = sorted(distinct.values(), key=lambda item: item.observed_at)
    age_level = evaluate_por_age(
        ordered[-1].observed_at,
        now,
        yellow_seconds=yellow_seconds,
        red_seconds=red_seconds,
    )
    change_level: RiskLevel | CoverageState = CoverageState.UNKNOWN
    if len(ordered) >= 2:
        change_level = evaluate_reserve_change(
            ordered[-2].reserves,
            ordered[-1].reserves,
            threshold=change_threshold,
        )
    recent = ordered[-recovery_read_count:]
    valid_recovery = len(recent) == recovery_read_count and all(
        evaluate_por_age(
            item.observed_at,
            now,
            yellow_seconds=yellow_seconds,
            red_seconds=red_seconds,
        )
        is RiskLevel.GREEN
        for item in recent
    )
    return PorEvaluation(age_level, change_level, valid_recovery)
