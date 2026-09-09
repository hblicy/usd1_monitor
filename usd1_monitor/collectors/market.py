from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol, Sequence

from usd1_monitor.http import HttpResponseError
from usd1_monitor.models import Observation


BINANCE_API_BASE = "https://api.binance.com"


class JsonHttpClient(Protocol):
    async def get_json(
        self, url: str, params: dict[str, object] | None = None
    ) -> object: ...


class MarketDataError(ValueError):
    """Raised when an exchange response cannot form a valid market observation."""


@dataclass(frozen=True)
class BookMetrics:
    best_bid: Decimal
    best_ask: Decimal
    mid: Decimal
    spread_bps: Decimal
    bid_depth_10bps: Decimal
    bid_depth_30bps: Decimal


@dataclass(frozen=True)
class SellSimulation:
    requested_base: Decimal
    filled_base: Decimal
    quote_received: Decimal
    vwap: Decimal | None
    terminal_price: Decimal | None
    fully_fillable: bool


def _decimal(value: object, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise MarketDataError(f"invalid {field}: {value!r}") from exc
    if not parsed.is_finite() or parsed < 0:
        raise MarketDataError(f"invalid {field}: {value!r}")
    return parsed


def simulate_sell(
    bids: Sequence[Sequence[str]], size: Decimal
) -> SellSimulation:
    if size <= 0:
        raise ValueError("sell size must be positive")

    remaining = size
    filled = Decimal(0)
    quote = Decimal(0)
    terminal: Decimal | None = None
    for level in bids:
        if len(level) < 2:
            raise MarketDataError("bid level must contain price and quantity")
        price = _decimal(level[0], "bid price")
        quantity = _decimal(level[1], "bid quantity")
        if price <= 0:
            raise MarketDataError("bid price must be positive")
        take = min(remaining, quantity)
        if take <= 0:
            continue
        filled += take
        quote += take * price
        remaining -= take
        terminal = price
        if remaining == 0:
            break

    return SellSimulation(
        requested_base=size,
        filled_base=filled,
        quote_received=quote,
        vwap=quote / filled if filled else None,
        terminal_price=terminal,
        fully_fillable=remaining == 0,
    )


def analyze_book(
    bids: Sequence[Sequence[str]], asks: Sequence[Sequence[str]]
) -> BookMetrics:
    if not bids or not asks:
        raise MarketDataError("order book must contain bids and asks")
    if len(bids[0]) < 2 or len(asks[0]) < 2:
        raise MarketDataError("book levels must contain price and quantity")

    best_bid = _decimal(bids[0][0], "best bid")
    best_ask = _decimal(asks[0][0], "best ask")
    if best_bid <= 0 or best_ask <= 0 or best_bid > best_ask:
        raise MarketDataError("order book top of book is invalid")

    mid = (best_bid + best_ask) / 2
    spread_bps = (best_ask - best_bid) / mid * Decimal(10_000)
    floor_10 = mid * Decimal("0.999")
    floor_30 = mid * Decimal("0.997")
    depth_10 = Decimal(0)
    depth_30 = Decimal(0)
    for level in bids:
        if len(level) < 2:
            raise MarketDataError("bid level must contain price and quantity")
        price = _decimal(level[0], "bid price")
        quantity = _decimal(level[1], "bid quantity")
        if price <= 0:
            raise MarketDataError("bid price must be positive")
        quote_value = price * quantity
        if price >= floor_10:
            depth_10 += quote_value
        if price >= floor_30:
            depth_30 += quote_value

    return BookMetrics(
        best_bid=best_bid,
        best_ask=best_ask,
        mid=mid,
        spread_bps=spread_bps,
        bid_depth_10bps=depth_10,
        bid_depth_30bps=depth_30,
    )


class BinanceMarketCollector:
    def __init__(
        self,
        http: JsonHttpClient,
        *,
        sell_sizes: Sequence[float | int | Decimal],
        depth_limit: int = 1000,
    ) -> None:
        if depth_limit != 1000:
            raise ValueError("Binance depth limit must be 1000")
        self._http = http
        self._sell_sizes = tuple(Decimal(str(size)) for size in sell_sizes)
        self._depth_limit = depth_limit

    async def collect_symbol(
        self, symbol: str, collected_at: datetime
    ) -> list[Observation]:
        try:
            exchange_info = await self._http.get_json(
                f"{BINANCE_API_BASE}/api/v3/exchangeInfo", {"symbol": symbol}
            )
        except HttpResponseError as exc:
            if exc.status == 400 and exc.error_code == -1121:
                return [
                    self._observation(
                        "market.symbol_trading",
                        symbol,
                        False,
                        "bool",
                        collected_at,
                        {"status": "ABSENT"},
                    )
                ]
            raise
        try:
            symbols = exchange_info["symbols"]  # type: ignore[index]
            if not isinstance(symbols, list):
                raise TypeError("symbols must be a list")
            symbol_info = next(
                (item for item in symbols if item.get("symbol") == symbol), None
            )
        except (AttributeError, KeyError, TypeError) as exc:
            raise MarketDataError(f"malformed Binance response for {symbol}") from exc

        status = "ABSENT" if symbol_info is None else symbol_info.get("status")
        if not isinstance(status, str):
            raise MarketDataError(f"malformed Binance status for {symbol}")
        if status != "TRADING":
            return [
                self._observation(
                    "market.symbol_trading",
                    symbol,
                    False,
                    "bool",
                    collected_at,
                    {"status": status},
                )
            ]

        depth = await self._http.get_json(
            f"{BINANCE_API_BASE}/api/v3/depth",
            {"symbol": symbol, "limit": self._depth_limit},
        )
        try:
            bids = depth["bids"]  # type: ignore[index]
            asks = depth["asks"]  # type: ignore[index]
        except (KeyError, TypeError) as exc:
            raise MarketDataError(f"malformed Binance depth for {symbol}") from exc

        metrics = analyze_book(bids, asks)
        observations = [
            self._observation("market.symbol_trading", symbol, status == "TRADING", "bool", collected_at, {"status": status}),
            self._observation("market.mid_price", symbol, metrics.mid, symbol[-4:], collected_at),
            self._observation("market.spread_bps", symbol, metrics.spread_bps, "bps", collected_at),
            self._observation("market.bid_depth_10bps", symbol, metrics.bid_depth_10bps, symbol[-4:], collected_at),
            self._observation("market.bid_depth_30bps", symbol, metrics.bid_depth_30bps, symbol[-4:], collected_at),
        ]
        for size in self._sell_sizes:
            result = simulate_sell(bids, size)
            size_label = (
                str(size.quantize(Decimal(1)))
                if size == size.to_integral_value()
                else format(size.normalize(), "f")
            )
            metadata = {
                "requested_base": float(result.requested_base),
                "filled_base": float(result.filled_base),
                "quote_received": float(result.quote_received),
                "fully_fillable": result.fully_fillable,
            }
            observations.extend(
                [
                    self._observation(
                        f"market.sell_{size_label}_vwap",
                        symbol,
                        result.vwap if result.vwap is not None else 0,
                        symbol[-4:],
                        collected_at,
                        metadata,
                    ),
                    self._observation(
                        f"market.sell_{size_label}_terminal_price",
                        symbol,
                        result.terminal_price if result.terminal_price is not None else 0,
                        symbol[-4:],
                        collected_at,
                        metadata,
                    ),
                ]
            )
        return observations

    @staticmethod
    def _observation(
        metric: str,
        symbol: str,
        value: Decimal | float | int | bool,
        unit: str,
        collected_at: datetime,
        metadata: dict[str, object] | None = None,
    ) -> Observation:
        return Observation(
            metric=metric,
            source="binance",
            scope=symbol,
            value=float(value),
            unit=unit,
            observed_at=collected_at,
            collected_at=collected_at,
            metadata=dict(metadata or {}),
        )
