from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from usd1_monitor.models import CoverageState, RiskLevel


RiskOrCoverage = RiskLevel | CoverageState


@dataclass(frozen=True)
class SupplyRiskInput:
    estimated_ratios: list[float | None]
    native_drop_24h: float | None
    market_level: RiskOrCoverage
    initial_coverage_level: RiskLevel = RiskLevel.GREEN


@dataclass(frozen=True)
class SupplyEvaluation:
    coverage_level: RiskOrCoverage
    native_supply_level: RiskOrCoverage


class BridgeDirection(StrEnum):
    NORMAL = "normal"
    OVERISSUED = "overissued"
    LOCKED_EXCESS = "locked_excess"


@dataclass(frozen=True)
class BridgeReading:
    issued: float
    locked: float


@dataclass(frozen=True)
class BridgeEvaluation:
    level: RiskLevel
    direction: BridgeDirection
    delta: float
    ratio_percent: float | None


def evaluate_supply(value: SupplyRiskInput) -> SupplyEvaluation:
    market_warning = value.market_level in (RiskLevel.YELLOW, RiskLevel.RED)
    valid_ratios = [ratio for ratio in value.estimated_ratios if ratio is not None]
    if len(valid_ratios) < 2:
        coverage: RiskOrCoverage = CoverageState.UNKNOWN
    else:
        previous, current = valid_ratios[-2:]
        if previous <= 0 or current <= 0:
            coverage = CoverageState.UNKNOWN
        elif previous < 99 and current < 99 and market_warning:
            coverage = RiskLevel.RED
        elif (
            previous < 100
            and current < 100
            or abs(current - previous) > 0.5
        ):
            coverage = RiskLevel.YELLOW
        else:
            coverage = RiskLevel.GREEN
        if (
            coverage is RiskLevel.GREEN
            and value.initial_coverage_level is not RiskLevel.GREEN
            and not (previous >= 100 and current >= 100)
        ):
            coverage = value.initial_coverage_level

    if value.native_drop_24h is None:
        native: RiskOrCoverage = CoverageState.UNKNOWN
    elif value.native_drop_24h >= 0.02:
        native = RiskLevel.RED if market_warning else RiskLevel.YELLOW
    else:
        native = RiskLevel.GREEN
    return SupplyEvaluation(coverage, native)


def _classify_bridge(reading: BridgeReading) -> BridgeEvaluation:
    delta = reading.issued - reading.locked
    if delta > 0:
        ratio = (
            None
            if reading.locked == 0
            else delta / reading.locked * 100
        )
        if reading.locked == 0:
            level = RiskLevel.RED
        elif delta > 1_000_000 and ratio > 1:
            level = RiskLevel.RED
        elif delta > 100_000 and ratio > 0.1:
            level = RiskLevel.YELLOW
        else:
            level = RiskLevel.GREEN
        direction = BridgeDirection.OVERISSUED
    elif delta < 0:
        magnitude = -delta
        ratio = (
            None
            if reading.issued == 0
            else magnitude / reading.issued * 100
        )
        level = (
            RiskLevel.YELLOW
            if magnitude > 1_000_000 and (ratio is None or ratio > 1)
            else RiskLevel.GREEN
        )
        direction = BridgeDirection.LOCKED_EXCESS
    else:
        level = RiskLevel.GREEN
        direction = BridgeDirection.NORMAL
        ratio = 0.0
    return BridgeEvaluation(level, direction, delta, ratio)


def evaluate_bridge_reconciliation(
    readings: list[BridgeReading],
    previous_level: RiskLevel,
) -> BridgeEvaluation:
    if not readings:
        return BridgeEvaluation(
            previous_level,
            BridgeDirection.NORMAL,
            0.0,
            None,
        )
    current = _classify_bridge(readings[-1])
    if len(readings) < 2:
        return BridgeEvaluation(
            previous_level,
            current.direction,
            current.delta,
            current.ratio_percent,
        )

    prior = _classify_bridge(readings[-2])
    same_direction = prior.direction is current.direction
    two_green = (
        prior.level is RiskLevel.GREEN
        and current.level is RiskLevel.GREEN
    )
    two_red = (
        same_direction
        and prior.level is RiskLevel.RED
        and current.level is RiskLevel.RED
    )
    two_warning = (
        same_direction
        and prior.level is not RiskLevel.GREEN
        and current.level is not RiskLevel.GREEN
    )
    if previous_level is RiskLevel.RED:
        level = RiskLevel.GREEN if two_green else RiskLevel.RED
    elif previous_level is RiskLevel.YELLOW:
        if two_red:
            level = RiskLevel.RED
        elif two_green:
            level = RiskLevel.GREEN
        else:
            level = RiskLevel.YELLOW
    elif two_red:
        level = RiskLevel.RED
    elif two_warning:
        level = RiskLevel.YELLOW
    else:
        level = RiskLevel.GREEN
    return BridgeEvaluation(
        level,
        current.direction,
        current.delta,
        current.ratio_percent,
    )
