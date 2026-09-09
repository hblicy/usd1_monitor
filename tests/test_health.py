from datetime import UTC, datetime, timedelta

import pytest

from usd1_monitor.engine.health import (
    CollectorHealth,
    evaluate_health,
    evaluate_health_with_recovery,
)
from usd1_monitor.models import RiskLevel
from usd1_monitor.scheduler import _record_health


NOW = datetime(2026, 9, 7, 4, 30, tzinfo=UTC)


def test_three_failures_are_yellow() -> None:
    health = CollectorHealth("binance", 3, NOW - timedelta(minutes=2), "timeout")

    assert evaluate_health(health, NOW, critical=True) is RiskLevel.YELLOW


def test_critical_source_stale_for_fifteen_minutes_is_red() -> None:
    health = CollectorHealth("por", 1, NOW - timedelta(minutes=16), "rpc timeout")

    assert evaluate_health(health, NOW, critical=True) is RiskLevel.RED


def test_successful_collector_is_green() -> None:
    health = CollectorHealth("binance", 0, NOW, None)

    assert evaluate_health(health, NOW, critical=True) is RiskLevel.GREEN


def test_critical_source_failing_since_start_is_red_after_fifteen_minutes() -> None:
    health = CollectorHealth(
        "por", 4, None, "offline",
        last_failure_at=NOW,
        first_failure_at=NOW - timedelta(minutes=16),
    )

    assert evaluate_health(health, NOW, critical=True) is RiskLevel.RED


def test_warned_collector_does_not_recover_on_first_success() -> None:
    health = CollectorHealth(
        "scheduler_evm_bsc",
        0,
        NOW,
        None,
        last_failure_at=NOW - timedelta(seconds=20),
    )

    assert evaluate_health_with_recovery(
        health,
        NOW,
        critical=True,
        previous_level=RiskLevel.YELLOW,
    ) is RiskLevel.YELLOW


def test_warned_collector_recovers_after_sixty_stable_seconds() -> None:
    health = CollectorHealth(
        "scheduler_evm_bsc",
        0,
        NOW,
        None,
        last_failure_at=NOW - timedelta(seconds=60),
    )

    assert evaluate_health_with_recovery(
        health,
        NOW,
        critical=True,
        previous_level=RiskLevel.YELLOW,
    ) is RiskLevel.GREEN


def test_unwarned_collector_stays_green_during_recovery_window() -> None:
    health = CollectorHealth(
        "scheduler_evm_bsc",
        0,
        NOW,
        None,
        last_failure_at=NOW - timedelta(seconds=20),
    )

    assert evaluate_health_with_recovery(
        health,
        NOW,
        critical=True,
        previous_level=RiskLevel.GREEN,
    ) is RiskLevel.GREEN


@pytest.mark.asyncio
async def test_collector_health_failure_and_recovery_are_persisted(storage) -> None:
    await storage.record_collector_failure("binance", NOW, "timeout")
    await storage.record_collector_failure("binance", NOW, "timeout again")
    failed = await storage.get_collector_health("binance")
    assert failed is not None
    assert failed.consecutive_failures == 2
    assert failed.last_error == "timeout again"
    assert failed.first_failure_at == NOW

    await storage.record_collector_success("binance", NOW + timedelta(minutes=1))
    recovered = await storage.get_collector_health("binance")
    assert recovered is not None
    assert recovered.consecutive_failures == 0
    assert recovered.last_success_at == NOW + timedelta(minutes=1)
    assert recovered.last_error is None
    assert recovered.first_failure_at is None


@pytest.mark.asyncio
async def test_record_health_waits_before_emitting_recovery(storage) -> None:
    for _ in range(3):
        await _record_health(
            storage,
            "scheduler_evm_bsc",
            NOW,
            success=False,
            error="timeout",
            critical=True,
        )

    await _record_health(
        storage,
        "scheduler_evm_bsc",
        NOW + timedelta(seconds=20),
        success=True,
        critical=True,
    )
    held = await storage.get_risk_state("health.scheduler_evm_bsc")
    assert held is not None and held.level is RiskLevel.YELLOW

    await _record_health(
        storage,
        "scheduler_evm_bsc",
        NOW + timedelta(seconds=60),
        success=True,
        critical=True,
    )
    recovered = await storage.get_risk_state("health.scheduler_evm_bsc")
    assert recovered is not None and recovered.level is RiskLevel.GREEN
