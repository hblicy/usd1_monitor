from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes import FakeNotifier, FakePorCollector, FakeSupplyCollector
from usd1_monitor.collectors.reserves import PorSnapshot
from usd1_monitor.models import Observation, RiskLevel
from usd1_monitor.config import SupplyConfig
from usd1_monitor.scheduler import (
    CombinedSupplySource,
    ReserveSupplyMonitor,
    SupplyBatch,
)
from usd1_monitor.engine.state import StateEngine


NOW = datetime(2026, 9, 7, 4, 0, tzinfo=UTC)


def supply_snapshot(scope: str, value: float, observed_at: datetime):
    from usd1_monitor.collectors.supply import SupplySnapshot

    metric = "supply.global" if scope == "global" else "supply.native"
    source = "defillama" if scope == "global" else "evm_rpc"
    observation = Observation(
        metric, source, scope, value, "USD1", observed_at, observed_at,
        quality="ESTIMATED_SOURCE" if scope == "global" else "FACT",
        metadata={
            "source_url": (
                "https://stablecoins.llama.fi/stablecoins?includePrices=true"
                if scope == "global"
                else f"https://{'etherscan.io' if scope == 'ethereum' else 'bscscan.com'}/block/123"
            )
        },
    )
    return SupplySnapshot(scope, value, observed_at, observation)


@pytest.mark.asyncio
async def test_combined_supply_sources_start_concurrently() -> None:
    import asyncio

    ethereum_release = asyncio.Event()
    later_sources_started = asyncio.Event()
    started_count = 0

    class Rpc:
        def __init__(self, blocked: bool = False) -> None:
            self.blocked = blocked

        async def call(self, method, params):
            nonlocal started_count
            started_count += 1
            if started_count >= 3:
                later_sources_started.set()
            if self.blocked:
                await later_sources_started.wait()
                ethereum_release.set()
            return hex(100)

    class Native:
        def __init__(self, chain: str) -> None:
            self.chain = chain

        async def collect(self, block, collected_at):
            return supply_snapshot(self.chain, 100, collected_at)

    class Global:
        async def collect(self, collected_at):
            nonlocal started_count
            started_count += 1
            if started_count >= 3:
                later_sources_started.set()
            return supply_snapshot("global", 200, collected_at)

    source = CombinedSupplySource(
        [
            (Rpc(blocked=True), Native("ethereum"), 12),
            (Rpc(), Native("bsc"), 15),
        ],
        Global(),
    )

    batch = await asyncio.wait_for(source.collect(NOW), timeout=0.2)

    assert ethereum_release.is_set()
    assert {item.scope for item in batch.snapshots} == {
        "ethereum", "bsc", "global"
    }


@pytest.mark.asyncio
async def test_slow_por_does_not_block_supply(storage) -> None:
    supply_started = __import__("asyncio").Event()

    class SlowPor:
        async def collect(self, collected_at):
            await supply_started.wait()
            raise RuntimeError("por unavailable")

    class QuickSupply:
        async def collect(self, collected_at):
            supply_started.set()
            return []

    monitor = ReserveSupplyMonitor(SlowPor(), QuickSupply(), storage, None)

    result = await __import__("asyncio").wait_for(
        monitor.check_once(now=NOW), timeout=0.2
    )

    assert result.success is False


@pytest.mark.asyncio
async def test_por_failure_does_not_block_supply(storage) -> None:
    por = FakePorCollector()
    supply = FakeSupplyCollector()
    por.queue_error(TimeoutError("oracle rpc unavailable"))
    supply.queue_global_supply(4_200_000_000)
    monitor = ReserveSupplyMonitor(por, supply, storage, FakeNotifier())

    result = await monitor.check_once()

    rows = await storage.latest_observations("supply.global", limit=1)
    assert rows[0].value == 4_200_000_000
    assert result.success is False
    assert any("oracle" in error for error in result.errors)


@pytest.mark.asyncio
async def test_fresh_reserves_and_supply_emit_estimated_coverage(storage) -> None:
    por = FakePorCollector()
    supply = FakeSupplyCollector()
    por_observations = (
        Observation("por.reserves", "por_oracle", "ethereum", 4_250_000_000, "USD", NOW, NOW),
        Observation("por.oracle_age_seconds", "por_oracle", "ethereum", 0, "seconds", NOW, NOW),
    )
    por.queue_snapshot(
        PorSnapshot(4_250_000_000, int(NOW.timestamp()), NOW, NOW, por_observations)
    )
    supply.queue_global_supply(4_200_000_000)
    monitor = ReserveSupplyMonitor(por, supply, storage, FakeNotifier())

    await monitor.check_once(now=NOW)

    rows = await storage.latest_observations(
        "supply.estimated_collateralization", limit=1
    )
    assert rows[0].value == pytest.approx(101.190476)
    assert rows[0].quality == "ESTIMATED"


@pytest.mark.asyncio
async def test_por_and_coverage_evaluations_include_source_links(storage) -> None:
    old = NOW - timedelta(hours=2)
    await storage.insert_observation(
        Observation(
            "por.reserves",
            "por_oracle",
            "ethereum",
            99,
            "USD",
            old,
            old,
            metadata={
                "source_urls": [
                    "https://etherscan.io/address/0xoracle#readContract",
                    "https://etherscan.io/block/100",
                ]
            },
        )
    )
    monitor = ReserveSupplyMonitor(
        FakePorCollector(), FakeSupplyCollector(), storage, None
    )

    por_evaluations = await monitor._por_evaluations(NOW)

    age = next(item for item in por_evaluations if item.rule_id == "por.age")
    assert age.evidence["source_urls"] == [
        "https://etherscan.io/address/0xoracle#readContract",
        "https://etherscan.io/block/100",
    ]

    await storage.insert_observation(
        Observation(
            "por.reserves",
            "por_oracle",
            "ethereum",
            99,
            "USD",
            NOW,
            NOW,
            metadata={"source_url": "https://etherscan.io/block/101"},
        )
    )
    await storage.insert_observation(
        Observation(
            "supply.global",
            "defillama",
            "global",
            100,
            "USD1",
            NOW,
            NOW,
            metadata={
                "source_url": "https://stablecoins.llama.fi/stablecoins?includePrices=true"
            },
        )
    )
    await storage.insert_observation(
        Observation(
            "supply.estimated_collateralization",
            "por+defillama",
            "global",
            99.5,
            "percent",
            NOW - timedelta(hours=1),
            NOW - timedelta(hours=1),
            quality="ESTIMATED",
        )
    )

    _, coverage_evaluations = await monitor._coverage_update(NOW)

    coverage = next(
        item
        for item in coverage_evaluations
        if item.rule_id == "supply.estimated_coverage"
    )
    assert coverage.evidence["source_urls"] == [
        "https://etherscan.io/block/101",
        "https://stablecoins.llama.fi/stablecoins?includePrices=true",
    ]


@pytest.mark.asyncio
async def test_coverage_is_created_when_supply_finishes_before_por(storage) -> None:
    supply_collected = __import__("asyncio").Event()

    class DelayedPor:
        async def collect(self, collected_at):
            await supply_collected.wait()
            observations = (Observation(
                "por.reserves", "por_oracle", "ethereum", 110,
                "USD", collected_at, collected_at,
            ),)
            return PorSnapshot(
                110, int(collected_at.timestamp()), collected_at,
                collected_at, observations,
            )

    class QuickSupply:
        async def collect(self, collected_at):
            supply_collected.set()
            return (supply_snapshot("global", 100, collected_at),)

    monitor = ReserveSupplyMonitor(DelayedPor(), QuickSupply(), storage, None)
    await monitor.check_once(now=NOW)

    ratio = await storage.latest_observation(
        "supply.estimated_collateralization", "global"
    )
    assert ratio is not None and ratio.value == pytest.approx(110)


@pytest.mark.asyncio
async def test_hourly_coverage_waits_for_current_por_after_supply_persists(
    storage,
) -> None:
    previous = NOW
    current = NOW + timedelta(hours=1)
    await storage.insert_observation(
        Observation(
            "por.reserves", "por_oracle", "ethereum", 100,
            "USD", previous, previous,
        )
    )
    await storage.insert_observation(
        Observation(
            "supply.global", "defillama", "global", 100,
            "USD1", previous, previous, quality="ESTIMATED_SOURCE",
        )
    )
    await storage.insert_observation(
        Observation(
            "supply.estimated_collateralization", "por+defillama", "global",
            100, "percent", previous, previous, quality="ESTIMATED",
        )
    )
    supply_collected = __import__("asyncio").Event()

    class DelayedPor:
        async def collect(self, collected_at):
            await supply_collected.wait()
            observations = (
                Observation(
                    "por.reserves", "por_oracle", "ethereum", 110,
                    "USD", collected_at, collected_at,
                ),
            )
            return PorSnapshot(
                110, int(collected_at.timestamp()), collected_at,
                collected_at, observations,
            )

    class QuickSupply:
        async def collect(self, collected_at):
            supply_collected.set()
            return (supply_snapshot("global", 100, collected_at),)

    monitor = ReserveSupplyMonitor(DelayedPor(), QuickSupply(), storage, None)
    await monitor.check_once(now=current)

    ratio = await storage.latest_observation(
        "supply.estimated_collateralization", "global"
    )
    assert ratio is not None and ratio.value == pytest.approx(110)


@pytest.mark.asyncio
async def test_same_cycle_inputs_replace_recent_coverage_point(storage) -> None:
    previous = NOW
    recent = NOW + timedelta(minutes=30)
    current = NOW + timedelta(hours=1)
    await storage.insert_observation(
        Observation(
            "por.reserves", "por_oracle", "ethereum", 100,
            "USD", previous, previous,
        )
    )
    await storage.insert_observation(
        Observation(
            "supply.global", "defillama", "global", 100,
            "USD1", previous, previous, quality="ESTIMATED_SOURCE",
        )
    )
    await storage.insert_observation(
        Observation(
            "supply.estimated_collateralization", "por+defillama", "global",
            100, "percent", recent, recent, quality="ESTIMATED",
        )
    )

    class CurrentPor:
        async def collect(self, collected_at):
            observations = (
                Observation(
                    "por.reserves", "por_oracle", "ethereum", 110,
                    "USD", collected_at, collected_at,
                ),
            )
            return PorSnapshot(
                110, int(collected_at.timestamp()), collected_at,
                collected_at, observations,
            )

    class CurrentSupply:
        async def collect(self, collected_at):
            return (supply_snapshot("global", 200, collected_at),)

    monitor = ReserveSupplyMonitor(CurrentPor(), CurrentSupply(), storage, None)

    await monitor.check_once(now=current)

    ratio = await storage.latest_observation(
        "supply.estimated_collateralization", "global"
    )
    assert ratio is not None
    assert ratio.value == pytest.approx(55)
    assert ratio.collected_at == current
    ratio_rows = await storage.latest_observations(
        "supply.estimated_collateralization", limit=10
    )
    assert [item.collected_at for item in ratio_rows] == [current]
    assert await storage.get_risk_state("supply.estimated_coverage") is None


@pytest.mark.asyncio
async def test_coverage_gap_does_not_count_as_consecutive_hourly_points(
    storage,
) -> None:
    previous = NOW - timedelta(hours=24)
    await storage.insert_observation(
        Observation(
            "supply.estimated_collateralization",
            "por+defillama",
            "global",
            99.8,
            "percent",
            previous,
            previous,
            quality="ESTIMATED",
        )
    )
    await storage.insert_observation(
        Observation(
            "por.reserves", "por_oracle", "ethereum", 99.7,
            "USD", NOW, NOW,
        )
    )
    await storage.insert_observation(
        Observation(
            "supply.global", "defillama", "global", 100,
            "USD1", NOW, NOW, quality="ESTIMATED_SOURCE",
        )
    )
    monitor = ReserveSupplyMonitor(
        FakePorCollector(),
        FakeSupplyCollector(),
        storage,
        None,
        supply_config=SupplyConfig(interval_seconds=3600),
    )

    _, evaluations = await monitor._coverage_update(NOW)

    assert not any(
        item.rule_id == "supply.estimated_coverage" for item in evaluations
    )


@pytest.mark.asyncio
async def test_failed_current_por_does_not_pair_old_por_with_current_supply(
    storage,
) -> None:
    previous = NOW
    current = NOW + timedelta(hours=1)
    await storage.insert_observation(
        Observation(
            "por.reserves", "por_oracle", "ethereum", 100,
            "USD", previous, previous,
        )
    )
    await storage.insert_observation(
        Observation(
            "supply.global", "defillama", "global", 100,
            "USD1", previous, previous, quality="ESTIMATED_SOURCE",
        )
    )

    class FailedPor:
        async def collect(self, collected_at):
            raise RuntimeError("offline")

    class CurrentSupply:
        async def collect(self, collected_at):
            return (supply_snapshot("global", 200, collected_at),)

    monitor = ReserveSupplyMonitor(FailedPor(), CurrentSupply(), storage, None)

    result = await monitor.check_once(now=current)

    assert result.success is False
    assert await storage.latest_observations(
        "supply.estimated_collateralization", limit=10
    ) == []


@pytest.mark.asyncio
async def test_stale_reserves_do_not_emit_estimated_coverage(storage) -> None:
    por = FakePorCollector()
    supply = FakeSupplyCollector()
    old = NOW - timedelta(minutes=76)
    observations = (
        Observation("por.reserves", "por_oracle", "ethereum", 4_250_000_000, "USD", old, NOW),
    )
    por.queue_snapshot(
        PorSnapshot(4_250_000_000, int(old.timestamp()), old, NOW, observations)
    )
    supply.queue_global_supply(4_200_000_000)
    monitor = ReserveSupplyMonitor(por, supply, storage, FakeNotifier())

    await monitor.check_once(now=NOW)

    assert await storage.latest_observations(
        "supply.estimated_collateralization", limit=1
    ) == []


@pytest.mark.asyncio
async def test_por_red_state_needs_two_fresh_reads_to_recover(storage) -> None:
    await storage.set_risk_state("por.age", RiskLevel.RED, NOW, NOW)
    await storage.insert_observation(
        Observation(
            "por.reserves", "por_oracle", "ethereum", 4_250_000_000,
            "USD", NOW + timedelta(minutes=5), NOW + timedelta(minutes=5)
        )
    )
    monitor = ReserveSupplyMonitor(
        FakePorCollector(), FakeSupplyCollector(), storage, FakeNotifier()
    )

    await monitor._evaluate_por(NOW + timedelta(minutes=5))

    assert (await storage.get_risk_state("por.age")).level is RiskLevel.RED


@pytest.mark.asyncio
async def test_restart_does_not_create_second_supply_point_within_hour(storage) -> None:
    first_por = FakePorCollector()
    first_por.queue_error(RuntimeError("por offline"))
    first_supply = FakeSupplyCollector()
    first_supply.queue_global_supply(4_200_000_000)
    await ReserveSupplyMonitor(
        first_por, first_supply, storage, None
    ).check_once(now=NOW)

    second_por = FakePorCollector()
    second_por.queue_error(RuntimeError("por offline"))
    second_supply = FakeSupplyCollector()
    second_supply.queue_global_supply(4_100_000_000)
    await ReserveSupplyMonitor(
        second_por, second_supply, storage, None
    ).check_once(now=NOW + timedelta(minutes=5))

    rows = await storage.latest_observations("supply.global", limit=10)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_partial_supply_failure_keeps_other_source_data(storage) -> None:
    class PartialSupply:
        async def collect(self, collected_at):
            observation = Observation(
                "supply.global", "defillama", "global", 4_200_000_000,
                "USD1", collected_at, collected_at,
                quality="ESTIMATED_SOURCE",
            )
            from usd1_monitor.collectors.supply import SupplySnapshot
            return SupplyBatch(
                (SupplySnapshot("global", observation.value, collected_at, observation),),
                (("ethereum", RuntimeError("rpc offline")),),
            )

    por = FakePorCollector()
    por.queue_error(RuntimeError("por offline"))
    result = await ReserveSupplyMonitor(
        por, PartialSupply(), storage, None
    ).check_once(now=NOW)

    assert (await storage.latest_observation("supply.global", "global")) is not None
    health = await storage.get_collector_health("supply_ethereum")
    assert health is not None and health.consecutive_failures == 1
    assert result.success is False


@pytest.mark.asyncio
async def test_native_drop_is_evaluated_when_defillama_fails(storage) -> None:
    baseline = NOW - timedelta(hours=24)
    for scope in ("ethereum", "bsc"):
        await storage.insert_observation(
            supply_snapshot(scope, 100, baseline).observation
        )
    await storage.set_risk_state("market.price", RiskLevel.YELLOW, NOW, NOW)
    await storage.insert_observation(
        Observation(
            "market.mid_price", "binance", "USD1USDT", 0.998,
            "USDT", NOW, NOW,
        )
    )

    class NativeOnlySupply:
        async def collect(self, collected_at):
            return SupplyBatch(
                (
                    supply_snapshot("ethereum", 95, collected_at),
                    supply_snapshot("bsc", 100, collected_at),
                ),
                (("defillama", RuntimeError("offline")),),
            )

    class FailedPor:
        async def collect(self, collected_at):
            raise RuntimeError("offline")

    monitor = ReserveSupplyMonitor(
        FailedPor(), NativeOnlySupply(), storage, None
    )

    await monitor.check_once(now=NOW)

    state = await storage.get_risk_state("supply.native_drop_24h")
    assert state is not None and state.level is RiskLevel.RED
    pending = await storage.pending_alerts()
    native_alert = next(
        item for item in pending if "supply.native_drop_24h" in item.content
    )
    assert "https://etherscan.io/block/123" in native_alert.content
    assert "https://bscscan.com/block/123" in native_alert.content
    assert "来源=未提供" not in native_alert.content


@pytest.mark.asyncio
async def test_native_drop_requires_fresh_value_for_each_chain(storage) -> None:
    baseline = NOW - timedelta(hours=24)
    for scope in ("ethereum", "bsc"):
        await storage.insert_observation(
            supply_snapshot(scope, 100, baseline).observation
        )
    await storage.insert_observation(
        supply_snapshot("ethereum", 95, NOW).observation
    )
    await storage.insert_observation(
        supply_snapshot("bsc", 95, NOW - timedelta(hours=2)).observation
    )
    monitor = ReserveSupplyMonitor(
        FakePorCollector(), FakeSupplyCollector(), storage, None
    )

    assert await monitor._native_drop_24h(NOW) is None


@pytest.mark.asyncio
async def test_partial_supply_failure_does_not_repeat_healthy_sources_each_minute(
    storage,
) -> None:
    class PartialSupply:
        calls = 0

        async def collect(self, collected_at):
            self.calls += 1
            return SupplyBatch(
                (supply_snapshot("ethereum", 100, collected_at),),
                (("defillama", RuntimeError("offline")),),
            )

    class FailedPor:
        async def collect(self, collected_at):
            raise RuntimeError("offline")

    supply = PartialSupply()
    monitor = ReserveSupplyMonitor(FailedPor(), supply, storage, None)

    await monitor.check_once(now=NOW)
    await monitor.check_once(now=NOW + timedelta(minutes=1))

    assert supply.calls == 1


@pytest.mark.asyncio
async def test_failed_global_supply_does_not_create_coverage_from_old_value(
    storage,
) -> None:
    await storage.insert_observation(
        Observation(
            "por.reserves", "por_oracle", "ethereum", 4_250_000_000,
            "USD", NOW, NOW,
        )
    )
    await storage.insert_observation(
        Observation(
            "supply.global", "defillama", "global", 4_200_000_000,
            "USD1", NOW, NOW, quality="ESTIMATED_SOURCE",
        )
    )

    class FailedSupply:
        async def collect(self, collected_at):
            return SupplyBatch((), (("defillama", RuntimeError("offline")),))

    por = FakePorCollector()
    por.queue_error(RuntimeError("offline"))
    monitor = ReserveSupplyMonitor(por, FailedSupply(), storage, None)

    await monitor.check_once(now=NOW + timedelta(hours=1))

    assert await storage.latest_observations(
        "supply.estimated_collateralization", limit=10
    ) == []
    assert monitor._last_supply_run == NOW + timedelta(hours=1)


@pytest.mark.asyncio
async def test_recovered_supply_source_clears_its_health_state(storage) -> None:
    from usd1_monitor.collectors.supply import SupplySnapshot

    class RecoveringSupply:
        calls = 0

        async def collect(self, collected_at):
            self.calls += 1
            if self.calls == 1:
                return SupplyBatch((), (("ethereum", RuntimeError("offline")),))
            observation = Observation(
                "supply.native", "evm_rpc", "ethereum", 1_000,
                "USD1", collected_at, collected_at,
            )
            return SupplyBatch(
                (SupplySnapshot("ethereum", 1_000, collected_at, observation),)
            )

    por = FakePorCollector()
    por.queue_error(RuntimeError("por offline"))
    por.queue_error(RuntimeError("por offline"))
    monitor = ReserveSupplyMonitor(
        por,
        RecoveringSupply(),
        storage,
        None,
        supply_config=SupplyConfig(interval_seconds=1),
    )

    await monitor.check_once(now=NOW)
    await monitor.check_once(now=NOW + timedelta(seconds=1))

    health = await storage.get_collector_health("supply_ethereum")
    assert health is not None
    assert health.consecutive_failures == 0


@pytest.mark.asyncio
async def test_por_reserve_change_needs_valid_recovery(storage) -> None:
    await storage.set_risk_state("por.reserve_change", RiskLevel.YELLOW, NOW, NOW)
    await storage.insert_observation(
        Observation(
            "por.reserves", "por_oracle", "ethereum", 100,
            "USD", NOW, NOW,
        )
    )
    await storage.insert_observation(
        Observation(
            "por.reserves", "por_oracle", "ethereum", 100,
            "USD", NOW, NOW + timedelta(minutes=5),
        )
    )
    monitor = ReserveSupplyMonitor(
        FakePorCollector(), FakeSupplyCollector(), storage, None
    )

    await monitor._evaluate_por(NOW + timedelta(minutes=5))

    state = await storage.get_risk_state("por.reserve_change")
    assert state is not None and state.level is RiskLevel.YELLOW


@pytest.mark.asyncio
async def test_por_reserve_change_does_not_recover_from_repolling_same_bundle(
    storage,
) -> None:
    old_bundle = NOW - timedelta(minutes=10)
    new_bundle = NOW - timedelta(minutes=5)
    await storage.set_risk_state(
        "por.reserve_change", RiskLevel.YELLOW, old_bundle, old_bundle
    )
    await storage.insert_observation(
        Observation(
            "por.reserves", "por_oracle", "ethereum", 100,
            "USD", old_bundle, old_bundle,
        )
    )
    await storage.insert_observation(
        Observation(
            "por.reserves", "por_oracle", "ethereum", 99,
            "USD", new_bundle, NOW - timedelta(minutes=1),
        )
    )
    await storage.insert_observation(
        Observation(
            "por.reserves", "por_oracle", "ethereum", 99,
            "USD", new_bundle, NOW,
        )
    )
    monitor = ReserveSupplyMonitor(
        FakePorCollector(), FakeSupplyCollector(), storage, None
    )

    await monitor._evaluate_por(NOW)

    state = await storage.get_risk_state("por.reserve_change")
    assert state is not None and state.level is RiskLevel.YELLOW


@pytest.mark.asyncio
async def test_por_reserve_change_recovers_after_two_distinct_stable_bundles(
    storage,
) -> None:
    old_bundle = NOW - timedelta(minutes=10)
    first_stable = NOW - timedelta(minutes=5)
    second_stable = NOW
    await storage.set_risk_state(
        "por.reserve_change", RiskLevel.YELLOW, old_bundle, old_bundle
    )
    for observed_at, reserves in (
        (old_bundle, 100),
        (first_stable, 99),
        (second_stable, 99),
    ):
        await storage.insert_observation(
            Observation(
                "por.reserves",
                "por_oracle",
                "ethereum",
                reserves,
                "USD",
                observed_at,
                observed_at,
            )
        )
    monitor = ReserveSupplyMonitor(
        FakePorCollector(), FakeSupplyCollector(), storage, None
    )

    await monitor._evaluate_por(NOW)

    state = await storage.get_risk_state("por.reserve_change")
    assert state is not None and state.level is RiskLevel.GREEN


@pytest.mark.asyncio
async def test_por_reserve_change_does_not_recover_during_continued_changes(
    storage,
) -> None:
    await storage.set_risk_state(
        "por.reserve_change", RiskLevel.YELLOW, NOW, NOW
    )
    for minutes, reserves in ((10, 100), (5, 99), (0, 98)):
        observed_at = NOW - timedelta(minutes=minutes)
        await storage.insert_observation(
            Observation(
                "por.reserves", "por_oracle", "ethereum", reserves,
                "USD", observed_at, observed_at,
            )
        )
    monitor = ReserveSupplyMonitor(
        FakePorCollector(), FakeSupplyCollector(), storage, None
    )

    await monitor._evaluate_por(NOW)

    state = await storage.get_risk_state("por.reserve_change")
    assert state is not None and state.level is RiskLevel.YELLOW


@pytest.mark.asyncio
async def test_stale_native_block_is_rejected_as_source_failure(storage) -> None:
    class StaleSupply:
        async def collect(self, collected_at):
            return SupplyBatch(
                (supply_snapshot(
                    "ethereum", 100, collected_at - timedelta(hours=2)
                ),)
            )

    class FailedPor:
        async def collect(self, collected_at):
            raise RuntimeError("offline")

    monitor = ReserveSupplyMonitor(FailedPor(), StaleSupply(), storage, None)

    await monitor.check_once(now=NOW)

    health = await storage.get_collector_health("supply_ethereum")
    assert health is not None and health.consecutive_failures == 1
    assert await storage.latest_observation("supply.native", "ethereum") is None


@pytest.mark.asyncio
async def test_por_observations_roll_back_when_state_write_fails(
    storage, monkeypatch
) -> None:
    por = FakePorCollector()
    por.queue_snapshot(
        PorSnapshot(
            100,
            int(NOW.timestamp()),
            NOW,
            NOW,
            (Observation(
                "por.reserves", "por_oracle", "ethereum", 100,
                "USD", NOW, NOW,
            ),),
        )
    )
    supply = FakeSupplyCollector()
    supply.queue_error(RuntimeError("supply offline"))
    original_apply = StateEngine.apply_uncommitted

    async def fail_por(self, evaluations, now):
        values = list(evaluations)
        if any(item.rule_id.startswith("por.") for item in values):
            raise RuntimeError("state write failed")
        return await original_apply(self, values, now)

    monkeypatch.setattr(StateEngine, "apply_uncommitted", fail_por)

    result = await ReserveSupplyMonitor(
        por, supply, storage, None
    ).check_once(now=NOW)

    assert result.success is False
    assert await storage.latest_observation("por.reserves", "ethereum") is None


@pytest.mark.asyncio
async def test_supply_observations_roll_back_when_coverage_state_fails(
    storage, monkeypatch
) -> None:
    await storage.insert_observation(
        Observation(
            "por.reserves", "por_oracle", "ethereum", 110,
            "USD", NOW, NOW,
        )
    )
    await storage.insert_observation(
        Observation(
            "supply.estimated_collateralization", "por+defillama", "global", 110,
            "percent", NOW - timedelta(hours=1), NOW - timedelta(hours=1),
            quality="ESTIMATED",
        )
    )
    por = FakePorCollector()
    por.queue_snapshot(
        PorSnapshot(
            110,
            int(NOW.timestamp()),
            NOW,
            NOW,
            (
                Observation(
                    "por.reserves", "por_oracle", "ethereum", 110,
                    "USD", NOW, NOW,
                ),
            ),
        )
    )
    supply = FakeSupplyCollector()
    supply.queue_global_supply(100)
    original_apply = StateEngine.apply_uncommitted

    async def fail_supply(self, evaluations, now):
        values = list(evaluations)
        if any(item.rule_id.startswith("supply.") for item in values):
            raise RuntimeError("state write failed")
        return await original_apply(self, values, now)

    monkeypatch.setattr(StateEngine, "apply_uncommitted", fail_supply)

    result = await ReserveSupplyMonitor(
        por, supply, storage, None
    ).check_once(now=NOW)

    assert result.success is False
    assert await storage.latest_observation("supply.global", "global") is None
