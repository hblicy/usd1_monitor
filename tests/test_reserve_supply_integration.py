from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes import FakeNotifier, FakePorCollector, FakeSupplyCollector
from usd1_monitor.collectors.reserves import PorSnapshot
from usd1_monitor.collectors.supply import SupplySnapshot
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


def por_snapshot_for(reserves: float, observed_at: datetime) -> PorSnapshot:
    observation = Observation(
        "por.reserves",
        "chainlink",
        "ethereum",
        reserves,
        "USD1",
        observed_at,
        observed_at,
    )
    return PorSnapshot(
        reserves,
        int(observed_at.timestamp()),
        observed_at,
        observed_at,
        (observation,),
    )


def aggregate_snapshot(
    metric: str,
    value: float,
    observed_at: datetime = NOW,
) -> SupplySnapshot:
    observation = Observation(
        metric,
        "onchain_multichain",
        "global",
        value,
        "USD1",
        observed_at,
        observed_at,
    )
    return SupplySnapshot("global", value, observed_at, observation)


def multichain_batch(
    *,
    native_total: float,
    bridged_total: float,
    locked_total: float,
    complete: bool,
    observed_at: datetime = NOW,
) -> SupplyBatch:
    totals = (
        aggregate_snapshot(
            "supply.multichain_total", native_total, observed_at
        ),
        aggregate_snapshot("supply.bridged_total", bridged_total, observed_at),
        aggregate_snapshot("bridge.locked_total", locked_total, observed_at),
        aggregate_snapshot(
            "bridge.issuance_delta",
            bridged_total - locked_total,
            observed_at,
        ),
    )
    return SupplyBatch(totals if complete else (), (), complete)


def partial_multichain_batch(*failed_ids: str) -> SupplyBatch:
    component = supply_snapshot("ethereum", 100, NOW)
    errors = tuple(
        (component_id, RuntimeError(f"{component_id} unavailable"))
        for component_id in failed_ids
    )
    return SupplyBatch((component,), errors, False)


@pytest.mark.asyncio
async def test_combined_supply_sources_start_concurrently() -> None:
    import asyncio

    global_started = asyncio.Event()

    class Multichain:
        async def collect(self, collected_at):
            await global_started.wait()
            from usd1_monitor.collectors.multichain_supply import (
                MultichainSupplyBatch,
            )

            return MultichainSupplyBatch((), (), (), True)

    class Global:
        async def collect(self, collected_at):
            global_started.set()
            return supply_snapshot("global", 200, collected_at)

    source = CombinedSupplySource(Multichain(), Global())

    batch = await asyncio.wait_for(source.collect(NOW), timeout=0.2)

    assert global_started.is_set()
    assert [item.scope for item in batch.snapshots] == ["global"]
    assert batch.multichain_complete is True


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
    supply.queue_batch(
        multichain_batch(
            native_total=4_200_000_000,
            bridged_total=1_000_000,
            locked_total=1_000_000,
            complete=True,
        )
    )
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
            "supply.multichain_total",
            "onchain_multichain",
            "global",
            100,
            "USD1",
            NOW,
            NOW,
            metadata={
                "source_url": "https://etherscan.io/token/usd1"
            },
        )
    )
    await storage.insert_observation(
        Observation(
            "supply.estimated_collateralization",
            "por+onchain_multichain",
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
        "https://etherscan.io/token/usd1",
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
            return multichain_batch(
                native_total=100,
                bridged_total=10,
                locked_total=10,
                complete=True,
                observed_at=collected_at,
            )

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
            "supply.multichain_total", "onchain_multichain", "global", 100,
            "USD1", previous, previous, quality="FACT",
        )
    )
    await storage.insert_observation(
        Observation(
            "supply.estimated_collateralization", "por+onchain_multichain", "global",
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
            return multichain_batch(
                native_total=100,
                bridged_total=10,
                locked_total=10,
                complete=True,
                observed_at=collected_at,
            )

    monitor = ReserveSupplyMonitor(DelayedPor(), QuickSupply(), storage, None)
    await monitor.check_once(now=current)

    ratio = await storage.latest_observation(
        "supply.estimated_collateralization", "global"
    )
    assert ratio is not None and ratio.value == pytest.approx(110)


@pytest.mark.asyncio
async def test_complete_supply_adds_one_coverage_point(storage) -> None:
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
            "supply.multichain_total", "onchain_multichain", "global", 100,
            "USD1", previous, previous, quality="FACT",
        )
    )
    await storage.insert_observation(
        Observation(
            "supply.estimated_collateralization", "por+onchain_multichain", "global",
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
            return multichain_batch(
                native_total=200,
                bridged_total=10,
                locked_total=10,
                complete=True,
                observed_at=collected_at,
            )

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
    assert [item.collected_at for item in ratio_rows] == [current, recent]
    state = await storage.get_risk_state("supply.estimated_coverage")
    assert state is not None and state.level is RiskLevel.YELLOW


@pytest.mark.asyncio
async def test_coverage_gap_does_not_count_as_consecutive_hourly_points(
    storage,
) -> None:
    previous = NOW - timedelta(hours=24)
    await storage.insert_observation(
        Observation(
            "supply.estimated_collateralization",
            "por+onchain_multichain",
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
            "supply.multichain_total", "onchain_multichain", "global", 100,
            "USD1", NOW, NOW, quality="FACT",
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
    first_supply.queue_batch(
        multichain_batch(
            native_total=4_200_000_000,
            bridged_total=1_000_000,
            locked_total=1_000_000,
            complete=True,
        )
    )
    await ReserveSupplyMonitor(
        first_por, first_supply, storage, None
    ).check_once(now=NOW)

    second_por = FakePorCollector()
    second_por.queue_error(RuntimeError("por offline"))
    second_supply = FakeSupplyCollector()
    second_supply.queue_batch(
        multichain_batch(
            native_total=4_100_000_000,
            bridged_total=1_000_000,
            locked_total=1_000_000,
            complete=True,
            observed_at=NOW + timedelta(minutes=5),
        )
    )
    await ReserveSupplyMonitor(
        second_por, second_supply, storage, None
    ).check_once(now=NOW + timedelta(minutes=5))

    rows = await storage.latest_observations(
        "supply.multichain_total",
        limit=10,
    )
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
    await storage.insert_observation(
        aggregate_snapshot(
            "supply.multichain_total",
            200,
            baseline,
        ).observation
    )
    await storage.set_risk_state("market.price", RiskLevel.YELLOW, NOW, NOW)
    await storage.insert_observation(
        Observation(
            "market.mid_price", "binance", "USD1USDT", 0.998,
            "USDT", NOW, NOW,
        )
    )

    class CompleteSupply:
        async def collect(self, collected_at):
            batch = multichain_batch(
                native_total=195,
                bridged_total=10,
                locked_total=10,
                complete=True,
                observed_at=collected_at,
            )
            return SupplyBatch(
                batch.snapshots,
                (("defillama", RuntimeError("offline")),),
                True,
            )

    class FailedPor:
        async def collect(self, collected_at):
            raise RuntimeError("offline")

    monitor = ReserveSupplyMonitor(
        FailedPor(), CompleteSupply(), storage, None
    )

    await monitor.check_once(now=NOW)

    state = await storage.get_risk_state("supply.native_drop_24h")
    assert state is not None and state.level is RiskLevel.RED
    pending = await storage.pending_alerts()
    native_alert = next(
        item for item in pending if "USD1 链上供应量在 24 小时内明显下降" in item.content
    )


@pytest.mark.asyncio
async def test_native_drop_requires_fresh_value_for_each_chain(storage) -> None:
    baseline = NOW - timedelta(hours=24)
    await storage.insert_observation(
        aggregate_snapshot(
            "supply.multichain_total", 200, baseline
        ).observation
    )
    await storage.insert_observation(
        aggregate_snapshot(
            "supply.multichain_total",
            190,
            NOW - timedelta(hours=2),
        ).observation
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
            "supply.estimated_collateralization", "por+onchain_multichain", "global", 110,
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
    supply.queue_batch(
        multichain_batch(
            native_total=100,
            bridged_total=10,
            locked_total=10,
            complete=True,
        )
    )
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
    assert (
        await storage.latest_observation(
            "supply.multichain_total", "global"
        )
        is None
    )


@pytest.mark.asyncio
async def test_complete_multichain_batch_drives_coverage(storage) -> None:
    por = FakePorCollector()
    por.queue_snapshot(por_snapshot_for(4_200, NOW))
    supply = FakeSupplyCollector()
    supply.queue_batch(
        multichain_batch(
            native_total=4_000,
            bridged_total=1_000,
            locked_total=1_000,
            complete=True,
        )
    )
    monitor = ReserveSupplyMonitor(por, supply, storage, None)

    await monitor.check_once(deliver=False, now=NOW)

    ratio = await storage.latest_observation(
        "supply.estimated_collateralization",
        "global",
    )
    assert ratio is not None and ratio.value == 105.0
    assert ratio.source == "por+onchain_multichain"


@pytest.mark.asyncio
async def test_partial_batch_persists_components_without_aggregates_or_risk(
    storage,
) -> None:
    supply = FakeSupplyCollector()
    supply.queue_batch(partial_multichain_batch("locked_aptos"))
    monitor = ReserveSupplyMonitor(
        FakePorCollector(), supply, storage, None
    )

    await monitor.check_once(deliver=False, now=NOW)

    assert (
        await storage.latest_observation("supply.native", "ethereum")
        is not None
    )
    assert (
        await storage.latest_observation(
            "supply.multichain_total", "global"
        )
        is None
    )
    assert (
        await storage.get_risk_state("supply.bridge_reconciliation")
        is None
    )


@pytest.mark.asyncio
async def test_por_only_cycle_does_not_duplicate_coverage_point(
    storage,
) -> None:
    por = FakePorCollector()
    por.queue_snapshot(por_snapshot_for(4_200, NOW))
    por.queue_snapshot(por_snapshot_for(4_200, NOW + timedelta(minutes=5)))
    supply = FakeSupplyCollector()
    supply.queue_batch(
        multichain_batch(
            native_total=4_000,
            bridged_total=1_000,
            locked_total=1_000,
            complete=True,
        )
    )
    monitor = ReserveSupplyMonitor(por, supply, storage, None)

    await monitor.check_once(deliver=False, now=NOW)
    await monitor.check_once(
        deliver=False,
        now=NOW + timedelta(minutes=5),
    )

    rows = await storage.latest_observations(
        "supply.estimated_collateralization",
        limit=10,
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_restart_due_time_ignores_recent_defillama_only_reading(
    storage,
) -> None:
    await storage.insert_observation(
        supply_snapshot("global", 4_000, NOW).observation
    )
    supply = FakeSupplyCollector()
    supply.queue_batch(partial_multichain_batch("locked_aptos"))
    monitor = ReserveSupplyMonitor(
        FakePorCollector(), supply, storage, None
    )

    result = await monitor.check_once(
        deliver=False,
        now=NOW + timedelta(minutes=1),
    )

    assert any("locked_aptos" in error for error in result.errors)


@pytest.mark.asyncio
async def test_bridge_evaluation_failure_rolls_back_complete_totals(
    storage,
    monkeypatch,
) -> None:
    supply = FakeSupplyCollector()
    supply.queue_batch(
        multichain_batch(
            native_total=4_000,
            bridged_total=1_100,
            locked_total=1_000,
            complete=True,
        )
    )
    monitor = ReserveSupplyMonitor(
        FakePorCollector(), supply, storage, None
    )

    async def fail_bridge(now: datetime):
        raise RuntimeError("bridge evaluation failed")

    monkeypatch.setattr(monitor, "_bridge_supply_evaluations", fail_bridge)
    await monitor.check_once(deliver=False, now=NOW)

    assert (
        await storage.latest_observation(
            "supply.multichain_total", "global"
        )
        is None
    )
    assert (
        await storage.latest_observation(
            "bridge.issuance_delta", "global"
        )
        is None
    )
