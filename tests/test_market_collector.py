import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from usd1_monitor.collectors.market import (
    BinanceMarketCollector,
    MarketDataError,
    analyze_book,
    simulate_sell,
)
from usd1_monitor.http import HttpResponseError
from tests.fakes import FakeHttp


def test_analyze_book_calculates_mid_spread_and_10bps_depth() -> None:
    bids = [["0.9990", "600000"], ["0.9985", "500000"]]
    asks = [["1.0010", "300000"]]

    result = analyze_book(bids, asks)

    assert result.mid == Decimal("1.0000")
    assert result.spread_bps == Decimal("20")
    assert result.bid_depth_10bps == Decimal("599400.0000")


def test_simulate_sell_reports_vwap_terminal_and_fillability() -> None:
    bids = [["0.999", "600000"], ["0.998", "500000"]]

    result = simulate_sell(bids, Decimal("1000000"))

    assert result.fully_fillable is True
    assert result.terminal_price == Decimal("0.998")
    assert result.vwap == Decimal("0.9986")


def test_simulate_sell_marks_truncated_book() -> None:
    result = simulate_sell([["0.999", "100"]], Decimal("1000000"))

    assert result.fully_fillable is False
    assert result.filled_base == Decimal("100")


@pytest.mark.asyncio
async def test_collector_uses_exact_endpoints_and_emits_market_observations() -> None:
    http = FakeHttp()
    exchange_url = "https://api.binance.com/api/v3/exchangeInfo"
    depth_url = "https://api.binance.com/api/v3/depth"
    depth = json.loads(
        (Path(__file__).parent / "fixtures" / "binance_depth.json").read_text(
            encoding="utf-8"
        )
    )
    http.queue_json(
        exchange_url,
        {"symbols": [{"symbol": "USD1USDT", "status": "TRADING"}]},
    )
    http.queue_json(depth_url, depth)
    collector = BinanceMarketCollector(http, sell_sizes=[1_000_000])
    collected_at = datetime(2026, 9, 7, 4, 0, tzinfo=UTC)

    observations = await collector.collect_symbol("USD1USDT", collected_at)

    assert http.calls == [
        (
            "GET",
            "https://api.binance.com/api/v3/exchangeInfo",
            {"symbol": "USD1USDT"},
        ),
        (
            "GET",
            "https://api.binance.com/api/v3/depth",
            {"symbol": "USD1USDT", "limit": 1000},
        ),
    ]
    by_metric = {observation.metric: observation for observation in observations}
    assert by_metric["market.symbol_trading"].value == 1.0
    assert by_metric["market.mid_price"].value == 1.0
    assert by_metric["market.sell_1000000_terminal_price"].value == 0.999
    assert by_metric["market.sell_1000000_terminal_price"].metadata["fully_fillable"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "depth",
    [
        {"bids": [], "asks": [["1.001", "1000"]]},
        {"bids": [["not-a-price", "1000"]], "asks": [["1.001", "1000"]]},
    ],
)
async def test_collector_rejects_empty_or_malformed_books(depth: object) -> None:
    http = FakeHttp()
    http.queue_json(
        "https://api.binance.com/api/v3/exchangeInfo",
        {"symbols": [{"symbol": "USD1USDT", "status": "TRADING"}]},
    )
    http.queue_json("https://api.binance.com/api/v3/depth", depth)
    collector = BinanceMarketCollector(http, sell_sizes=[1_000_000])

    with pytest.raises(MarketDataError):
        await collector.collect_symbol(
            "USD1USDT", datetime(2026, 9, 7, 4, 0, tzinfo=UTC)
        )


@pytest.mark.asyncio
async def test_missing_symbol_emits_non_trading_without_requesting_depth() -> None:
    http = FakeHttp()
    exchange_url = "https://api.binance.com/api/v3/exchangeInfo"
    http.queue_json(exchange_url, {"symbols": []})
    collector = BinanceMarketCollector(http, sell_sizes=[1_000_000])

    observations = await collector.collect_symbol(
        "USD1USDT", datetime(2026, 9, 7, 4, 0, tzinfo=UTC)
    )

    assert [item.metric for item in observations] == ["market.symbol_trading"]
    assert observations[0].value == 0.0
    assert observations[0].metadata["status"] == "ABSENT"
    assert len(http.calls) == 1


@pytest.mark.asyncio
async def test_binance_invalid_symbol_http_error_is_an_absent_market() -> None:
    http = FakeHttp()
    exchange_url = "https://api.binance.com/api/v3/exchangeInfo"
    http.queue_error(
        exchange_url,
        HttpResponseError("GET", exchange_url, 400, -1121),
    )
    collector = BinanceMarketCollector(http, sell_sizes=[1_000_000])

    observations = await collector.collect_symbol(
        "USD1FDUSD", datetime(2026, 9, 7, 4, 0, tzinfo=UTC)
    )

    assert [item.metric for item in observations] == ["market.symbol_trading"]
    assert observations[0].value == 0.0
    assert observations[0].metadata["status"] == "ABSENT"
