import asyncio
from datetime import UTC, datetime

import pytest

from usd1_monitor.collectors.multichain_supply import MultichainSupplySource
from usd1_monitor.collectors.supply import ComponentBatch, SupplySnapshot
from usd1_monitor.models import Observation


NOW = datetime(2026, 9, 10, 4, 0, tzinfo=UTC)


def component_snapshot(
    component_id: str,
    metric: str,
    scope: str,
    value: float,
) -> SupplySnapshot:
    observation = Observation(
        metric,
        "test",
        scope,
        value,
        "USD1",
        NOW,
        NOW,
        metadata={"component_id": component_id},
    )
    return SupplySnapshot(scope, value, NOW, observation)


class StaticSource:
    def __init__(self, snapshot: SupplySnapshot) -> None:
        self.snapshot = snapshot
        self.component_ids = frozenset(
            {snapshot.observation.metadata["component_id"]}
        )

    async def collect(self, collected_at: datetime) -> ComponentBatch:
        return ComponentBatch((self.snapshot,))


class FailingSource:
    def __init__(self, component_id: str) -> None:
        self.component_ids = frozenset({component_id})

    async def collect(self, collected_at: datetime) -> ComponentBatch:
        return ComponentBatch(
            (),
            ((next(iter(self.component_ids)), RuntimeError("failed")),),
        )


def component_sources_for(
    *, native: list[float], bridged: list[float], locked: list[float]
) -> list[StaticSource]:
    native_scopes = (
        "ethereum",
        "bsc",
        "tron",
        "solana",
        "aptos",
        "tempo",
    )
    bridged_scopes = ("plume", "ab", "monad", "mantle", "morph")
    locked_scopes = ("ethereum", "bsc", "solana", "aptos", "tempo")
    snapshots = [
        *(
            component_snapshot(
                f"native_{scope}", "supply.native", scope, value
            )
            for scope, value in zip(native_scopes, native, strict=True)
        ),
        *(
            component_snapshot(
                f"bridged_{scope}", "supply.bridged", scope, value
            )
            for scope, value in zip(bridged_scopes, bridged, strict=True)
        ),
        *(
            component_snapshot(
                f"locked_{scope}", "bridge.locked", scope, value
            )
            for scope, value in zip(locked_scopes, locked, strict=True)
        ),
    ]
    return [StaticSource(snapshot) for snapshot in snapshots]


def component_sources_for_complete_batch() -> list[StaticSource]:
    return component_sources_for(
        native=[100, 200, 300, 400, 500, 600],
        bridged=[10, 20, 30, 40, 50],
        locked=[11, 21, 31, 41, 51],
    )


class ConcurrencyGauge:
    def __init__(self) -> None:
        self.current = 0
        self.maximum = 0
        self.release = asyncio.Event()


class WaitingSource:
    def __init__(self, gauge: ConcurrencyGauge, component_id: str) -> None:
        self.gauge = gauge
        self.component_ids = frozenset({component_id})

    async def collect(self, collected_at: datetime) -> ComponentBatch:
        self.gauge.current += 1
        self.gauge.maximum = max(self.gauge.maximum, self.gauge.current)
        if self.gauge.maximum == 4:
            self.gauge.release.set()
        await self.gauge.release.wait()
        await asyncio.sleep(0)
        self.gauge.current -= 1
        return ComponentBatch(())


@pytest.mark.asyncio
async def test_complete_batch_aggregates_without_double_counting_bridged(
) -> None:
    source = MultichainSupplySource(
        component_sources_for(
            native=[100, 200, 300, 400, 500, 600],
            bridged=[10, 20, 30, 40, 50],
            locked=[11, 21, 31, 41, 51],
        )
    )

    batch = await source.collect(NOW)

    assert batch.complete is True
    totals = {
        item.observation.metric: item.supply for item in batch.totals
    }
    assert totals == {
        "supply.multichain_total": 2_100,
        "supply.bridged_total": 150,
        "bridge.locked_total": 155,
        "bridge.issuance_delta": -5,
    }


@pytest.mark.asyncio
async def test_one_missing_component_prevents_all_aggregate_values() -> None:
    sources = component_sources_for_complete_batch()
    sources[-2] = FailingSource("locked_aptos")

    batch = await MultichainSupplySource(sources).collect(NOW)

    assert batch.complete is False
    assert batch.totals == ()
    assert batch.errors[0][0] == "locked_aptos"


@pytest.mark.asyncio
async def test_source_concurrency_never_exceeds_four() -> None:
    gauge = ConcurrencyGauge()
    sources = [
        WaitingSource(gauge, f"source_{index}") for index in range(11)
    ]

    await MultichainSupplySource(
        sources,
        required_component_ids=frozenset(),
    ).collect(NOW)

    assert gauge.maximum == 4
