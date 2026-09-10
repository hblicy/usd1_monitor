from __future__ import annotations

from datetime import datetime
from typing import Protocol

from eth_abi import encode

from usd1_monitor.collectors.reserves import selector
from usd1_monitor.collectors.supply import (
    ComponentBatch,
    SupplyDataError,
    SupplySnapshot,
    _decode_uint,
)
from usd1_monitor.models import Observation
from usd1_monitor.supply_assets import EvmSupplySpec


class RpcClient(Protocol):
    async def call(self, method: str, params: list) -> object: ...


class EvmChainSupplyCollector:
    def __init__(
        self,
        chain: str,
        rpc: RpcClient,
        *,
        specs: tuple[EvmSupplySpec, ...],
        confirmation_depth: int = 0,
    ) -> None:
        self.chain = chain
        self._rpc = rpc
        self._specs = specs
        self._confirmation_depth = confirmation_depth
        self.component_ids = frozenset(item.component_id for item in specs)

    async def collect(self, collected_at: datetime) -> ComponentBatch:
        latest_raw = await self._rpc.call("eth_blockNumber", [])
        try:
            block = int(str(latest_raw), 16) - self._confirmation_depth
        except (TypeError, ValueError) as exc:
            raise SupplyDataError(
                f"{self.chain} block number is malformed"
            ) from exc
        if block < 0:
            raise SupplyDataError(
                f"{self.chain} has no confirmed supply block"
            )

        snapshots: list[SupplySnapshot] = []
        errors: list[tuple[str, Exception]] = []
        for spec in self._specs:
            try:
                snapshots.append(
                    await self._read(spec, block, collected_at)
                )
            except Exception as exc:
                errors.append((spec.component_id, exc))
        return ComponentBatch(tuple(snapshots), tuple(errors))

    async def _read(
        self,
        spec: EvmSupplySpec,
        block: int,
        collected_at: datetime,
    ) -> SupplySnapshot:
        if spec.holder_address is None:
            data = selector("totalSupply()")
        else:
            data = selector("balanceOf(address)") + encode(
                ["address"], [spec.holder_address]
            ).hex()
        raw = await self._rpc.call(
            "eth_call",
            [{"to": spec.token_address, "data": data}, hex(block)],
        )
        amount = _decode_uint(
            raw, "uint256", spec.component_id
        ) / (10**spec.decimals)
        if amount < 0 or (spec.metric == "supply.native" and amount == 0):
            raise SupplyDataError(f"{spec.component_id} amount is invalid")
        observation = Observation(
            spec.metric,
            "evm_rpc",
            spec.scope,
            float(amount),
            "USD1",
            collected_at,
            collected_at,
            quality="FACT",
            metadata={
                "block": block,
                "decimals": spec.decimals,
                "component_id": spec.component_id,
                "source_url": f"{spec.explorer_url}/block/{block}",
            },
        )
        return SupplySnapshot(
            spec.scope,
            float(amount),
            collected_at,
            observation,
        )
