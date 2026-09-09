from datetime import UTC, datetime
from pathlib import Path

import pytest

from usd1_monitor.collectors.announcements import (
    BinanceAnnouncementCollector,
    OfficialPageCollector,
    parse_bitgo_items,
    parse_occ_items,
    parse_wlfi_items,
)
from usd1_monitor.collectors.market import BinanceMarketCollector
from usd1_monitor.collectors.reserves import PorCollector
from usd1_monitor.collectors.supply import (
    DefiLlamaSupplyCollector,
    NativeSupplyCollector,
)
from usd1_monitor.config import load_config
from usd1_monitor.http import AsyncHttpClient
from usd1_monitor.rpc import JsonRpcClient


@pytest.fixture
def config():
    return load_config(Path("config.example.yaml"), environ={})


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_binance_books_have_explicit_status(config) -> None:
    async with AsyncHttpClient(config.http) as http:
        collector = BinanceMarketCollector(
            http,
            sell_sizes=[1_000_000],
            depth_limit=config.market.depth_limit,
        )
        for symbol in config.market.symbols:
            values = await collector.collect_symbol(symbol, datetime.now(UTC))
            by_metric = {item.metric: item for item in values}
            assert "status" in by_metric["market.symbol_trading"].metadata
            assert by_metric["market.mid_price"].value > 0


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_evm_chain_ids_code_and_native_supply(config) -> None:
    async with AsyncHttpClient(config.http) as http:
        for chain, expected_id in (("ethereum", 1), ("bsc", 56)):
            chain_config = getattr(config.chains, chain)
            rpc = JsonRpcClient(chain_config.rpc_urls, http)
            assert int(await rpc.call("eth_chainId", []), 16) == expected_id
            code = await rpc.call(
                "eth_getCode", [chain_config.token_address, "latest"]
            )
            assert isinstance(code, str) and code not in {"0x", "0x0"}
            latest = int(await rpc.call("eth_blockNumber", []), 16)
            result = await NativeSupplyCollector(
                chain, rpc, chain_config.token_address
            ).collect(latest - chain_config.confirmation_depth, datetime.now(UTC))
            assert result.supply > 0


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_por_and_defillama_are_positive(config) -> None:
    async with AsyncHttpClient(config.http) as http:
        eth = config.chains.ethereum
        rpc = JsonRpcClient(eth.rpc_urls, http)
        latest = int(await rpc.call("eth_blockNumber", []), 16)
        por = await PorCollector(rpc, config.por.address).collect(
            latest - eth.confirmation_depth, datetime.now(UTC)
        )
        assert por.oracle_timestamp > 0
        assert por.observed_at.timestamp() <= datetime.now(UTC).timestamp() + 300
        assert por.reserves > 0
        global_supply = await DefiLlamaSupplyCollector(http).collect(
            datetime.now(UTC)
        )
        assert global_supply.supply > 0


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_official_pages_have_structural_entries(config) -> None:
    parsers = {
        "bitgo": parse_bitgo_items,
        "wlfi": parse_wlfi_items,
        "occ": parse_occ_items,
    }
    async with AsyncHttpClient(config.http) as http:
        binance_items = await BinanceAnnouncementCollector(
            config.information.binance.url,
            http,
        ).collect(datetime.now(UTC))
        assert isinstance(binance_items, list)
        for source, parser in parsers.items():
            url = getattr(config.information, source).url
            items = await OfficialPageCollector(
                source, url, http, parser
            ).collect(datetime.now(UTC))
            assert isinstance(items, list)
            assert items
            if source == "bitgo":
                latest = items[-1]
                assert "parse_error" not in latest.metadata
                assert latest.metadata["tokens_outstanding"] > 0
                assert latest.metadata["redemption_assets"] > 0
