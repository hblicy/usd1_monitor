from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from usd1_monitor.models import RiskLevel


EXPECTED_SYMBOLS = {"USD1USDT", "USD1USDC"}
BINANCE_DEPTH_SOURCE_URL = "https://api.binance.com/api/v3/depth"
BINANCE_TRADING_SOURCE_URL = "https://api.binance.com/api/v3/exchangeInfo"


@dataclass(frozen=True)
class MarketSnapshot:
    observed_at: datetime
    mids: dict[str, float]
    trading: dict[str, bool]
    one_million_terminal: dict[str, float]
    one_million_fillable: dict[str, bool]
    exit_capacity: dict[str, dict[str, dict[str, Any]]] = field(
        default_factory=dict
    )


@dataclass(frozen=True)
class MarketEvaluation:
    price_level: RiskLevel
    liquidity_level: RiskLevel
    trading_level: RiskLevel
    severe_level: RiskLevel
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)


def _seconds_since(start: datetime | None, now: datetime) -> float:
    return 0 if start is None else (now - start).total_seconds()


def evaluate_market(
    history: list[MarketSnapshot],
    *,
    yellow_price: float = 0.997,
    yellow_seconds: int = 900,
    red_price: float = 0.995,
    red_seconds: int = 300,
    severe_price: float = 0.99,
    severe_seconds: int = 3600,
    recovery_price: float = 0.998,
    recovery_seconds: int = 900,
    max_gap_seconds: int = 90,
    initial_price_level: RiskLevel = RiskLevel.GREEN,
    initial_liquidity_level: RiskLevel = RiskLevel.GREEN,
    initial_trading_level: RiskLevel = RiskLevel.GREEN,
    initial_severe_level: RiskLevel = RiskLevel.GREEN,
) -> MarketEvaluation:
    if not history:
        return MarketEvaluation(
            initial_price_level,
            initial_liquidity_level,
            initial_trading_level,
            initial_severe_level,
            {},
        )

    ordered = sorted(history, key=lambda item: item.observed_at)
    price_level = initial_price_level
    liquidity_level = initial_liquidity_level
    trading_level = initial_trading_level
    severe_level = initial_severe_level
    yellow_started: datetime | None = None
    red_started: datetime | None = None
    severe_started: datetime | None = None
    price_recovery_started: datetime | None = None
    liquidity_recovery_started: datetime | None = None
    liquidity_bad_count = 0
    trading_good_count = 0
    trading_bad_count = 0
    previous_at: datetime | None = None

    for item in ordered:
        if not item.trading:
            raise ValueError("market snapshot must contain trading data")
        if bool(item.one_million_terminal) != bool(item.one_million_fillable):
            raise ValueError("market snapshot has incomplete liquidity data")

        gap = (
            previous_at is not None
            and (item.observed_at - previous_at).total_seconds() > max_gap_seconds
        )
        if gap:
            yellow_started = None
            red_started = None
            severe_started = None
            price_recovery_started = None
            liquidity_recovery_started = None
            liquidity_bad_count = 0
            trading_good_count = 0
            trading_bad_count = 0
        previous_at = item.observed_at

        has_price = bool(item.mids)
        yellow_now = has_price and any(price < yellow_price for price in item.mids.values())
        red_now = has_price and any(price < red_price for price in item.mids.values())
        severe_now = has_price and any(
            price < severe_price for price in item.mids.values()
        )
        healthy_price = (
            EXPECTED_SYMBOLS.issubset(item.mids)
            and all(item.trading.get(symbol) is True for symbol in EXPECTED_SYMBOLS)
            and all(
                item.mids[symbol] >= recovery_price
                for symbol in EXPECTED_SYMBOLS
            )
        )

        yellow_started = (
            yellow_started or item.observed_at if yellow_now else None
        )
        red_started = red_started or item.observed_at if red_now else None
        severe_started = (
            severe_started or item.observed_at if severe_now else None
        )
        price_recovery_started = (
            price_recovery_started or item.observed_at if healthy_price else None
        )

        if (
            red_now
            and _seconds_since(red_started, item.observed_at) >= red_seconds
        ):
            price_level = RiskLevel.RED
        elif (
            yellow_now
            and _seconds_since(yellow_started, item.observed_at) >= yellow_seconds
            and price_level is not RiskLevel.RED
        ):
            price_level = RiskLevel.YELLOW
        elif (
            healthy_price
            and _seconds_since(price_recovery_started, item.observed_at)
            >= recovery_seconds
        ):
            price_level = RiskLevel.GREEN

        if (
            severe_now
            and _seconds_since(severe_started, item.observed_at)
            >= severe_seconds
        ):
            severe_level = RiskLevel.RED
        elif (
            healthy_price
            and _seconds_since(price_recovery_started, item.observed_at)
            >= recovery_seconds
        ):
            severe_level = RiskLevel.GREEN

        has_liquidity = bool(item.one_million_fillable)
        liquidity_bad = has_liquidity and (any(
            not fillable for fillable in item.one_million_fillable.values()
        ) or any(
            terminal < red_price
            for terminal in item.one_million_terminal.values()
        ))
        liquidity_healthy = (
            EXPECTED_SYMBOLS.issubset(item.one_million_fillable)
            and EXPECTED_SYMBOLS.issubset(item.one_million_terminal)
            and all(item.trading.get(symbol) is True for symbol in EXPECTED_SYMBOLS)
            and all(
                item.one_million_fillable[symbol]
                and item.one_million_terminal[symbol] >= yellow_price
                for symbol in EXPECTED_SYMBOLS
            )
        )
        liquidity_bad_count = liquidity_bad_count + 1 if liquidity_bad else 0
        liquidity_recovery_started = (
            liquidity_recovery_started or item.observed_at
            if liquidity_healthy
            else None
        )
        if liquidity_bad_count >= 2:
            liquidity_level = RiskLevel.RED
        elif (
            liquidity_healthy
            and _seconds_since(liquidity_recovery_started, item.observed_at)
            >= recovery_seconds
        ):
            liquidity_level = RiskLevel.GREEN

        trading_bad = not all(item.trading.values())
        trading_bad_count = trading_bad_count + 1 if trading_bad else 0
        trading_good_count = trading_good_count + 1 if not trading_bad else 0
        if trading_bad_count >= 2:
            trading_level = RiskLevel.RED
        elif trading_good_count >= 2:
            trading_level = RiskLevel.GREEN

    latest = ordered[-1]
    depth_source_urls = [
        f"{BINANCE_DEPTH_SOURCE_URL}?symbol={symbol}&limit=1000"
        for symbol in sorted(EXPECTED_SYMBOLS)
    ]
    trading_source_urls = [
        f"{BINANCE_TRADING_SOURCE_URL}?symbol={symbol}"
        for symbol in sorted(EXPECTED_SYMBOLS)
    ]
    evidence = {
        "market.price": {
            "mids": latest.mids,
            "yellow_threshold": yellow_price,
            "red_threshold": red_price,
            "exit_capacity": latest.exit_capacity,
            "data_time": latest.observed_at.isoformat(),
            "source_url": depth_source_urls[0],
            "source_urls": depth_source_urls,
        },
        "market.price.severe": {
            "mids": latest.mids,
            "severe_threshold": severe_price,
            "severe_duration_seconds": severe_seconds,
            "exit_capacity": latest.exit_capacity,
            "data_time": latest.observed_at.isoformat(),
            "source_url": depth_source_urls[0],
            "source_urls": depth_source_urls,
        },
        "market.liquidity": {
            "terminal_prices": latest.one_million_terminal,
            "fully_fillable": latest.one_million_fillable,
            "exit_capacity": latest.exit_capacity,
            "threshold": red_price,
            "data_time": latest.observed_at.isoformat(),
            "source_url": depth_source_urls[0],
            "source_urls": depth_source_urls,
        },
        "market.trading": {
            "trading": latest.trading,
            "data_time": latest.observed_at.isoformat(),
            "source_url": trading_source_urls[0],
            "source_urls": trading_source_urls,
        },
    }
    return MarketEvaluation(
        price_level=price_level,
        liquidity_level=liquidity_level,
        trading_level=trading_level,
        severe_level=severe_level,
        evidence=evidence,
    )
