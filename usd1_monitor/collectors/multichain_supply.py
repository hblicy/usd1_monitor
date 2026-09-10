from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import datetime

from usd1_monitor.collectors.supply import (
    ComponentBatch,
    ComponentSource,
    SupplyDataError,
    SupplySnapshot,
)
from usd1_monitor.models import Observation


REQUIRED_COMPONENT_IDS = frozenset(
    {
        "native_ethereum",
        "native_bsc",
        "native_tron",
        "native_solana",
        "native_aptos",
        "native_tempo",
        "bridged_plume",
        "bridged_ab",
        "bridged_monad",
        "bridged_mantle",
        "bridged_morph",
        "locked_ethereum",
        "locked_bsc",
        "locked_solana",
        "locked_aptos",
        "locked_tempo",
    }
)


@dataclass(frozen=True)
class MultichainSupplyBatch:
    components: tuple[SupplySnapshot, ...]
    totals: tuple[SupplySnapshot, ...]
    errors: tuple[tuple[str, Exception], ...]
    complete: bool


class MultichainSupplySource:
    def __init__(
        self,
        sources: list[ComponentSource],
        *,
        required_component_ids: frozenset[str] = REQUIRED_COMPONENT_IDS,
    ) -> None:
        self._sources = tuple(sources)
        self._required = required_component_ids
        self._semaphore = asyncio.Semaphore(4)

    @property
    def required_component_ids(self) -> frozenset[str]:
        return self._required

    @property
    def max_concurrency(self) -> int:
        return 4

    async def _collect_one(
        self,
        source: ComponentSource,
        now: datetime,
    ) -> ComponentBatch:
        async with self._semaphore:
            return await source.collect(now)

    async def collect(
        self,
        collected_at: datetime,
    ) -> MultichainSupplyBatch:
        results = await asyncio.gather(
            *(
                self._collect_one(source, collected_at)
                for source in self._sources
            ),
            return_exceptions=True,
        )
        components: list[SupplySnapshot] = []
        errors: list[tuple[str, Exception]] = []
        for source, result in zip(self._sources, results, strict=True):
            if isinstance(result, Exception):
                errors.extend(
                    (component_id, result)
                    for component_id in source.component_ids
                )
            elif isinstance(result, BaseException):
                raise result
            else:
                components.extend(result.snapshots)
                errors.extend(result.errors)

        indexed = {
            str(item.observation.metadata["component_id"]): item
            for item in components
        }
        complete = (
            not errors
            and len(components) == len(self._required)
            and set(indexed) == set(self._required)
        )
        totals = (
            self._totals(indexed, collected_at)
            if complete and self._required
            else ()
        )
        return MultichainSupplyBatch(
            tuple(components),
            tuple(totals),
            tuple(errors),
            complete,
        )

    @staticmethod
    def _totals(
        indexed: dict[str, SupplySnapshot],
        collected_at: datetime,
    ) -> tuple[SupplySnapshot, ...]:
        native = math.fsum(
            item.supply
            for component_id, item in indexed.items()
            if component_id.startswith("native_")
        )
        bridged = math.fsum(
            item.supply
            for component_id, item in indexed.items()
            if component_id.startswith("bridged_")
        )
        locked = math.fsum(
            item.supply
            for component_id, item in indexed.items()
            if component_id.startswith("locked_")
        )
        if native <= 0 or bridged < 0 or locked < 0:
            raise SupplyDataError(
                "multichain aggregate values are invalid"
            )

        def aggregate(metric: str, value: float) -> SupplySnapshot:
            observation = Observation(
                metric,
                "onchain_multichain",
                "global",
                value,
                "USD1",
                collected_at,
                collected_at,
                quality="FACT",
                metadata={"components": sorted(indexed)},
            )
            return SupplySnapshot(
                "global",
                value,
                collected_at,
                observation,
            )

        return (
            aggregate("supply.multichain_total", native),
            aggregate("supply.bridged_total", bridged),
            aggregate("bridge.locked_total", locked),
            aggregate("bridge.issuance_delta", bridged - locked),
        )
