from datetime import UTC, datetime, timedelta

from usd1_monitor.engine.reserve_rules import (
    PorReading,
    evaluate_por,
    evaluate_por_age,
    evaluate_reserve_change,
)
from usd1_monitor.models import CoverageState, RiskLevel


NOW = datetime(2026, 9, 7, 4, 0, tzinfo=UTC)


def test_por_age_boundaries() -> None:
    assert evaluate_por_age(NOW - timedelta(seconds=3600), NOW) is RiskLevel.GREEN
    assert evaluate_por_age(NOW - timedelta(seconds=3601), NOW) is RiskLevel.YELLOW
    assert evaluate_por_age(NOW - timedelta(seconds=7201), NOW) is RiskLevel.RED


def test_reserve_relative_change_is_strictly_above_half_percent() -> None:
    assert evaluate_reserve_change(100, 99.5) is RiskLevel.GREEN
    assert evaluate_reserve_change(100, 99.49) is RiskLevel.YELLOW


def test_missing_por_readings_are_unknown() -> None:
    assert evaluate_por([], NOW).age_level is CoverageState.UNKNOWN


def test_two_fresh_reads_recover_reserve_health() -> None:
    readings = [
        PorReading(4_200_000_000, NOW - timedelta(minutes=10)),
        PorReading(4_200_000_000, NOW - timedelta(minutes=5)),
    ]

    result = evaluate_por(readings, NOW)

    assert result.age_level is RiskLevel.GREEN
    assert result.valid_recovery is True


def test_por_recovery_rejects_duplicate_oracle_timestamp() -> None:
    reading = PorReading(100.0, NOW)

    result = evaluate_por([reading, reading], NOW + timedelta(minutes=1))

    assert result.valid_recovery is False


def test_two_collections_of_same_fresh_oracle_bundle_cannot_recover() -> None:
    oracle_time = NOW - timedelta(minutes=5)
    readings = [
        PorReading(100.0, oracle_time, NOW - timedelta(minutes=1)),
        PorReading(100.0, oracle_time, NOW),
    ]

    result = evaluate_por(readings, NOW)

    assert result.valid_recovery is False
