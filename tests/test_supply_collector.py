import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from eth_abi import encode

from tests.fakes import FakeHttp, FakeRpc
from usd1_monitor.collectors.supply import (
    DefiLlamaSupplyCollector,
    NativeSupplyCollector,
    SupplyDataError,
    choose_defillama_asset,
    estimated_coverage,
)


NOW = datetime(2026, 9, 7, 4, 0, tzinfo=UTC)
TOKEN = "0x" + "11" * 20


def test_choose_world_liberty_usd1_and_exclude_unitas() -> None:
    assets = [
        {"id": "101", "symbol": "USD1", "name": "Unitas"},
        {
            "id": "202",
            "symbol": "USD1",
            "name": "World Liberty Financial USD",
        },
    ]

    assert choose_defillama_asset(assets)["id"] == "202"


def test_duplicate_world_liberty_match_is_rejected() -> None:
    assets = [
        {
            "id": "202",
            "symbol": "USD1",
            "name": "World Liberty Financial USD",
        },
        {
            "id": "203",
            "symbol": "USD1",
            "name": "World Liberty Financial USD",
        },
    ]

    with pytest.raises(SupplyDataError, match="exactly one"):
        choose_defillama_asset(assets)


def test_estimated_coverage_is_labeled_estimate() -> None:
    result = estimated_coverage(
        reserves=4_250_000_000, global_supply=4_200_000_000
    )

    assert result.ratio_percent == pytest.approx(101.190476)
    assert result.quality == "ESTIMATED"


@pytest.mark.asyncio
async def test_native_supply_decodes_decimals_and_total_supply() -> None:
    rpc = FakeRpc()
    rpc.result("eth_call", "0x" + encode(["uint8"], [18]).hex())
    rpc.result(
        "eth_call", "0x" + encode(["uint256"], [4_200_000_000 * 10**18]).hex()
    )
    rpc.result("eth_getBlockByNumber", {"timestamp": hex(int(NOW.timestamp()))})

    result = await NativeSupplyCollector("ethereum", rpc, TOKEN).collect(123, NOW)

    assert result.supply == 4_200_000_000
    assert result.observation.quality == "FACT"
    assert result.observed_at == NOW
    assert all(call[-1] == hex(123) for call in rpc.calls_for("eth_call"))
    assert result.observation.metadata["source_url"] == (
        "https://etherscan.io/block/123"
    )


@pytest.mark.asyncio
async def test_native_supply_uses_safe_block_timestamp_as_observed_time() -> None:
    rpc = FakeRpc()
    rpc.result("eth_call", "0x" + encode(["uint8"], [18]).hex())
    rpc.result(
        "eth_call", "0x" + encode(["uint256"], [100 * 10**18]).hex()
    )
    block_time = NOW.replace(hour=2)
    rpc.result(
        "eth_getBlockByNumber", {"timestamp": hex(int(block_time.timestamp()))}
    )

    result = await NativeSupplyCollector("ethereum", rpc, TOKEN).collect(123, NOW)

    assert result.observed_at == block_time
    assert result.observation.observed_at == block_time


@pytest.mark.asyncio
async def test_native_supply_rejects_decimals_call_failure() -> None:
    rpc = FakeRpc()
    rpc.result("eth_call", RuntimeError("revert"))

    with pytest.raises(RuntimeError, match="revert"):
        await NativeSupplyCollector("bsc", rpc, TOKEN).collect(123, NOW)


@pytest.mark.asyncio
async def test_defillama_collector_uses_exact_identity() -> None:
    body = json.loads(
        (Path(__file__).parent / "fixtures" / "defillama_stablecoins.json").read_text(
            encoding="utf-8"
        )
    )
    http = FakeHttp()
    url = "https://stablecoins.llama.fi/stablecoins"
    http.queue_json(url, body)

    result = await DefiLlamaSupplyCollector(http).collect(NOW)

    assert result.supply == 4_200_000_000
    assert result.observation.quality == "ESTIMATED_SOURCE"
    assert result.observation.metadata["source_url"] == (
        "https://stablecoins.llama.fi/stablecoins?includePrices=true"
    )
    assert http.calls == [("GET", url, {"includePrices": "true"})]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "assets",
    [
        [{"symbol": "USD1", "name": "Unitas", "circulating": {"peggedUSD": 1}}],
        [{"symbol": "USD1", "name": "World Liberty Financial USD", "circulating": {}}],
        [{"symbol": "USD1", "name": "World Liberty Financial USD", "circulating": {"peggedUSD": 0}}],
    ],
)
async def test_defillama_rejects_wrong_identity_or_invalid_supply(assets) -> None:
    http = FakeHttp()
    url = "https://stablecoins.llama.fi/stablecoins"
    http.queue_json(url, {"peggedAssets": assets})

    with pytest.raises(SupplyDataError):
        await DefiLlamaSupplyCollector(http).collect(NOW)
