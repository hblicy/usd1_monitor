from datetime import UTC, datetime

import pytest

from tests.fakes import FakeRpc
from usd1_monitor.collectors.multichain_evm import EvmChainSupplyCollector
from usd1_monitor.supply_assets import LOCKED_ETHEREUM, NATIVE_ETHEREUM


NOW = datetime(2026, 9, 10, 4, 0, tzinfo=UTC)


def hex_word(value: int) -> str:
    return f"0x{value:064x}"


@pytest.mark.asyncio
async def test_evm_chain_reads_supply_and_pool_at_one_block() -> None:
    rpc = FakeRpc()
    rpc.result("eth_blockNumber", "0x64")
    rpc.result("eth_call", hex_word(5_000_000 * 10**18))
    rpc.result("eth_call", hex_word(1_250_000 * 10**18))
    collector = EvmChainSupplyCollector(
        "ethereum",
        rpc,
        specs=(NATIVE_ETHEREUM, LOCKED_ETHEREUM),
        confirmation_depth=3,
    )

    batch = await collector.collect(NOW)

    assert [item.supply for item in batch.snapshots] == [
        5_000_000,
        1_250_000,
    ]
    assert all(
        call[-1] == "0x61" for call in rpc.calls_for("eth_call")
    )
    assert len(rpc.calls_for("eth_blockNumber")) == 1
    assert not rpc.calls_for("eth_getLogs")


@pytest.mark.asyncio
async def test_evm_chain_keeps_successful_component_when_pool_call_fails(
) -> None:
    rpc = FakeRpc()
    rpc.result("eth_blockNumber", "0x64")
    rpc.result("eth_call", hex_word(5_000_000 * 10**18))
    rpc.result("eth_call", RuntimeError("pool unavailable"))
    collector = EvmChainSupplyCollector(
        "ethereum",
        rpc,
        specs=(NATIVE_ETHEREUM, LOCKED_ETHEREUM),
        confirmation_depth=3,
    )

    batch = await collector.collect(NOW)

    assert [item.scope for item in batch.snapshots] == ["ethereum"]
    assert batch.errors[0][0] == "locked_ethereum"


@pytest.mark.asyncio
async def test_evm_chain_uses_pinned_decimals_without_decimals_call() -> None:
    rpc = FakeRpc()
    rpc.result("eth_blockNumber", "0x64")
    rpc.result("eth_call", hex_word(7 * 10**18))

    batch = await EvmChainSupplyCollector(
        "ethereum",
        rpc,
        specs=(NATIVE_ETHEREUM,),
    ).collect(NOW)

    assert batch.snapshots[0].supply == 7
    assert len(rpc.calls_for("eth_call")) == 1
