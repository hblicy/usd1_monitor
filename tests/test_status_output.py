from datetime import UTC, datetime, timedelta

import pytest

from usd1_monitor.cli import _print_status
from usd1_monitor.engine.state import StateEngine
from usd1_monitor.models import RiskLevel, RuleEvaluation
from usd1_monitor.models import Observation
from usd1_monitor.scheduler import NOT_MONITORED
from usd1_monitor.engine.aggregate import business_overall, health_overall


async def seed_status_pillars(storage, now: datetime, *, reserves_at=None) -> None:
    for metric, scope, value, unit, observed_at, metadata in (
        (
            "custody.binance_share_lower_bound",
            "global",
            0.40,
            "ratio",
            now,
            {"max_age_seconds": 1200},
        ),
        (
            "por.reserves",
            "ethereum",
            4_200_000_000,
            "USD",
            reserves_at or now,
            {},
        ),
        (
            "supply.multichain_total",
            "global",
            4_100_000_000,
            "USD1",
            now,
            {},
        ),
        (
            "redemption.channel_status",
            "global",
            0,
            "risk_level",
            now,
            {"max_age_seconds": 900},
        ),
    ):
        await storage.insert_observation(
            Observation(
                metric,
                "test",
                scope,
                value,
                unit,
                observed_at,
                now,
                quality="FACT",
                metadata=metadata,
            )
        )


def test_status_names_every_non_monitored_capability() -> None:
    expected = {
        "private_exchange_account",
        "active_conversion_probe",
        "tron_solana_aptos_tempo_bridges",
        "social_media_sentiment",
        "defi_liquidations",
    }

    assert set(NOT_MONITORED) == expected
    assert "web_dashboard" not in NOT_MONITORED


def test_full_multichain_reconciliation_is_no_longer_not_monitored() -> None:
    assert "full_multichain_supply_reconciliation" not in NOT_MONITORED
    assert "tron_solana_aptos_tempo_bridges" in NOT_MONITORED


@pytest.mark.asyncio
async def test_status_prints_multichain_aggregate_metrics(
    storage,
    capsys,
) -> None:
    now = datetime(2026, 9, 10, tzinfo=UTC)
    for metric, value in (
        ("supply.multichain_total", 4_200_000_000),
        ("supply.bridged_total", 1_250_000_000),
        ("bridge.locked_total", 1_249_650_000),
        ("bridge.issuance_delta", 350_000),
    ):
        await storage.insert_observation(
            Observation(
                metric,
                "onchain_multichain",
                "global",
                value,
                "USD1",
                now,
                now,
            )
        )

    await _print_status(storage)

    output = capsys.readouterr().out
    for metric in (
        "supply.multichain_total",
        "supply.bridged_total",
        "bridge.locked_total",
        "bridge.issuance_delta",
    ):
        assert f"metric {metric}: FACT" in output


@pytest.mark.asyncio
async def test_evm_event_only_affects_overall_during_active_window(storage) -> None:
    now = datetime(2026, 9, 7, tzinfo=UTC)
    await storage.set_risk_state(
        "event.evm.ethereum.0xmint:0",
        RiskLevel.RED,
        now,
        now,
    )

    states = await storage.list_risk_states()
    assert business_overall(states, now=now) is RiskLevel.RED
    assert business_overall(states, now=now + timedelta(hours=2)) is RiskLevel.GREEN


@pytest.mark.asyncio
async def test_irreversible_evm_event_remains_in_overall_after_window(storage) -> None:
    now = datetime(2026, 9, 7, tzinfo=UTC)
    await storage.set_risk_state(
        "evm.event.ethereum.snapshot:100:implementation",
        RiskLevel.RED,
        now,
        now,
    )

    states = await storage.list_risk_states()

    assert business_overall(states, now=now + timedelta(hours=2)) is RiskLevel.RED


@pytest.mark.asyncio
async def test_empty_status_is_unknown_not_green(storage, capsys) -> None:
    await _print_status(storage)

    output = capsys.readouterr().out
    assert "business_overall: UNKNOWN" in output
    assert "monitor_health: UNKNOWN" in output


@pytest.mark.asyncio
async def test_status_treats_unresolved_dead_letter_as_yellow(storage, capsys) -> None:
    now = datetime(2026, 9, 7, tzinfo=UTC)
    await StateEngine(storage).apply(
        [RuleEvaluation("market.price", RiskLevel.YELLOW)], now
    )
    pending = (await storage.pending_alerts())[0]
    assert await storage.claim_alert_delivery(pending.id) is True
    await storage.record_delivery_result(pending.id, now, error="timeout")
    assert await storage.claim_alert_delivery(pending.id) is True
    await storage.record_delivery_result(pending.id, now, error="timeout")

    await _print_status(storage)

    output = capsys.readouterr().out
    assert "monitor_health: YELLOW" in output
    assert "failed_alerts: 1" in output


@pytest.mark.asyncio
async def test_status_separates_business_health_and_failed_delivery(
    storage, capsys
) -> None:
    now = datetime(2026, 9, 7, tzinfo=UTC)
    await storage.set_risk_state("market.price", RiskLevel.RED, now, now)
    await storage.set_risk_state("health.por", RiskLevel.YELLOW, now, now)
    await StateEngine(storage).apply(
        [RuleEvaluation("market.liquidity", RiskLevel.YELLOW)], now
    )
    pending = (await storage.pending_alerts())[0]
    assert await storage.claim_alert_delivery(pending.id) is True
    await storage.record_delivery_result(pending.id, now, error="timeout")
    assert await storage.claim_alert_delivery(pending.id) is True
    await storage.record_delivery_result(pending.id, now, error="timeout")

    await _print_status(storage)

    output = capsys.readouterr().out
    assert "business_overall: RED" in output
    assert "monitor_health: YELLOW" in output
    assert "failed_alerts: 1" in output


@pytest.mark.asyncio
async def test_por_age_affects_monitor_health_not_business_overall(
    storage, capsys
) -> None:
    now = datetime(2026, 9, 10, tzinfo=UTC)
    await storage.set_risk_state("market.price", RiskLevel.GREEN, now, now)
    await storage.set_risk_state("por.age", RiskLevel.RED, now, now)

    states = await storage.list_risk_states()
    assert business_overall(states, now=now) is RiskLevel.GREEN
    assert health_overall(states) is RiskLevel.RED

    await _print_status(storage, now=now)

    output = capsys.readouterr().out
    assert "business_overall: UNKNOWN" in output
    assert "monitor_health: RED" in output


@pytest.mark.asyncio
async def test_status_uses_three_pillars_for_green(storage, capsys) -> None:
    now = datetime(2026, 9, 10, tzinfo=UTC)
    await storage.set_risk_state("market.price", RiskLevel.GREEN, now, now)
    await seed_status_pillars(storage, now)

    await _print_status(storage, now=now)

    output = capsys.readouterr().out
    assert "business_overall: GREEN" in output
    assert "missing_pillars: -" in output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("max_age_seconds", "reserve_age_seconds", "expected_level"),
    [(900, 900, "UNKNOWN"), (3600, 1800, "GREEN")],
)
async def test_status_uses_configured_por_coverage_freshness(
    storage,
    capsys,
    max_age_seconds: int,
    reserve_age_seconds: int,
    expected_level: str,
) -> None:
    now = datetime(2026, 9, 10, tzinfo=UTC)
    await storage.set_risk_state("market.price", RiskLevel.GREEN, now, now)
    await seed_status_pillars(
        storage,
        now,
        reserves_at=now - timedelta(seconds=reserve_age_seconds),
    )

    await _print_status(
        storage,
        now=now,
        coverage_max_age_seconds=max_age_seconds,
    )

    assert f"business_overall: {expected_level}" in capsys.readouterr().out


@pytest.mark.asyncio
@pytest.mark.parametrize("max_age_seconds", [0, True])
async def test_status_rejects_invalid_por_coverage_freshness(
    storage,
    max_age_seconds: object,
) -> None:
    with pytest.raises(ValueError, match="coverage_max_age_seconds"):
        await _print_status(
            storage,
            coverage_max_age_seconds=max_age_seconds,
        )


@pytest.mark.asyncio
async def test_status_hides_estimated_coverage_when_por_is_stale(storage, capsys) -> None:
    now = datetime(2026, 9, 10, tzinfo=UTC)
    await storage.set_risk_state("market.price", RiskLevel.GREEN, now, now)
    await seed_status_pillars(storage, now, reserves_at=now - timedelta(minutes=30))
    await storage.insert_observation(
        Observation(
            "supply.estimated_collateralization",
            "por+onchain_multichain",
            "global",
            102.44,
            "percent",
            now,
            now,
            quality="ESTIMATED",
        )
    )

    await _print_status(storage, now=now)

    output = capsys.readouterr().out
    assert "business_overall: UNKNOWN" in output
    assert "missing_pillars: coverage" in output
    assert "metric por.reserves: FACT value=4.2e+09" in output
    assert "metric supply.estimated_collateralization: UNKNOWN scope=global" in output


@pytest.mark.asyncio
async def test_status_known_red_precedes_missing_pillars(storage, capsys) -> None:
    now = datetime(2026, 9, 10, tzinfo=UTC)
    await storage.set_risk_state("market.price", RiskLevel.RED, now, now)

    await _print_status(storage, now=now)

    output = capsys.readouterr().out
    assert "business_overall: RED" in output
    assert "missing_pillars: concentration,coverage,redemption" in output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "observed_at",
    [
        datetime(2026, 9, 10),
        datetime(2026, 9, 10, 0, 0, 1, tzinfo=UTC),
    ],
)
async def test_status_rejects_invalid_critical_pillar_time(
    storage,
    capsys,
    observed_at: datetime,
) -> None:
    now = datetime(2026, 9, 10, tzinfo=UTC)
    await storage.set_risk_state("market.price", RiskLevel.GREEN, now, now)
    await seed_status_pillars(storage, now, reserves_at=observed_at)

    with pytest.raises(ValueError, match="observation timestamp"):
        await _print_status(storage, now=now)


@pytest.mark.asyncio
async def test_status_displays_times_in_configured_timezone(storage, capsys) -> None:
    now = datetime(2026, 9, 7, 4, 0, tzinfo=UTC)
    await storage.set_risk_state("market.price", RiskLevel.GREEN, now, now)

    await _print_status(storage, timezone_name="Asia/Shanghai")

    assert "2026-09-07T12:00:00+08:00" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_status_distinguishes_estimated_and_unknown_metrics(storage, capsys) -> None:
    now = datetime(2026, 9, 7, 4, 0, tzinfo=UTC)
    await seed_status_pillars(storage, now)
    await storage.insert_observation(
        Observation(
            "supply.estimated_collateralization", "por+defillama", "global",
            100.5, "percent", now, now, quality="ESTIMATED",
        )
    )

    await _print_status(storage, now=now)

    output = capsys.readouterr().out
    assert "metric supply.estimated_collateralization: ESTIMATED value=100.5" in output
    assert "metric supply.native: UNKNOWN scope=ethereum" in output
