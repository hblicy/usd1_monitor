from usd1_monitor.engine.supply_rules import SupplyRiskInput, evaluate_supply
from usd1_monitor.models import CoverageState, RiskLevel


def test_estimated_ratio_below_100_is_only_yellow_by_itself() -> None:
    result = evaluate_supply(
        SupplyRiskInput(
            [99.8, 99.7], native_drop_24h=0.0, market_level=RiskLevel.GREEN
        )
    )

    assert result.coverage_level is RiskLevel.YELLOW


def test_ratio_below_99_and_market_warning_is_red() -> None:
    result = evaluate_supply(
        SupplyRiskInput(
            [98.8, 98.7],
            native_drop_24h=0.0,
            market_level=RiskLevel.YELLOW,
        )
    )

    assert result.coverage_level is RiskLevel.RED


def test_two_percent_native_drop_and_market_warning_is_red() -> None:
    result = evaluate_supply(
        SupplyRiskInput(
            [101.0, 101.0],
            native_drop_24h=0.02,
            market_level=RiskLevel.YELLOW,
        )
    )

    assert result.native_supply_level is RiskLevel.RED


def test_boundaries_do_not_over_trigger() -> None:
    exactly_full = evaluate_supply(
        SupplyRiskInput([100.0, 100.0], 0.0, RiskLevel.GREEN)
    )
    exactly_ninety_nine = evaluate_supply(
        SupplyRiskInput([99.0, 99.0], 0.0, RiskLevel.YELLOW)
    )
    exactly_half_point = evaluate_supply(
        SupplyRiskInput([100.5, 100.0], 0.0, RiskLevel.GREEN)
    )

    assert exactly_full.coverage_level is RiskLevel.GREEN
    assert exactly_ninety_nine.coverage_level is RiskLevel.YELLOW
    assert exactly_half_point.coverage_level is RiskLevel.GREEN


def test_missing_history_and_stale_market_do_not_become_red() -> None:
    missing = evaluate_supply(
        SupplyRiskInput([99.0], None, CoverageState.UNKNOWN)
    )
    stale_market = evaluate_supply(
        SupplyRiskInput([98.0, 98.0], 0.03, CoverageState.UNKNOWN)
    )

    assert missing.coverage_level is CoverageState.UNKNOWN
    assert missing.native_supply_level is CoverageState.UNKNOWN
    assert stale_market.coverage_level is RiskLevel.YELLOW
    assert stale_market.native_supply_level is RiskLevel.YELLOW


def test_coverage_recovery_requires_two_good_hourly_points() -> None:
    first = evaluate_supply(
        SupplyRiskInput(
            [99.5, 100.1], None, RiskLevel.GREEN,
            initial_coverage_level=RiskLevel.YELLOW,
        )
    )
    second = evaluate_supply(
        SupplyRiskInput(
            [100.1, 100.2], None, RiskLevel.GREEN,
            initial_coverage_level=RiskLevel.YELLOW,
        )
    )

    assert first.coverage_level is RiskLevel.YELLOW
    assert second.coverage_level is RiskLevel.GREEN
