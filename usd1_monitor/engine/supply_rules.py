from __future__ import annotations

from dataclasses import dataclass

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
