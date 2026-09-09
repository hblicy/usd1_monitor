from pathlib import Path
from datetime import datetime

import pytest_asyncio

from usd1_monitor.storage import Storage
from usd1_monitor.models import Observation


@pytest_asyncio.fixture
async def storage(tmp_path: Path):
    instance = Storage(tmp_path / "monitor.db")
    await instance.open()
    try:
        yield instance
    finally:
        await instance.close()


class FakeMarketCollector:
    def __init__(self) -> None:
        self._prices: list[tuple[float, datetime]] = []
        self._active: tuple[float, datetime] | None = None

    def queue_price(self, price: float, observed_at: datetime) -> None:
        self._prices.append((price, observed_at))

    async def collect_symbol(
        self, symbol: str, collected_at: datetime
    ) -> list[Observation]:
        if symbol == "USD1USDT":
            self._active = self._prices.pop(0)
        if self._active is None:
            raise AssertionError("USD1USDT must be collected before USD1USDC")
        price, observed_at = self._active
        observations = [
            Observation(
                "market.symbol_trading", "binance", symbol, 1.0, "bool", observed_at, collected_at
            ),
            Observation(
                "market.mid_price", "binance", symbol, price, symbol[-4:], observed_at, collected_at
            ),
        ]
        for size in (1_000_000, 5_000_000, 20_000_000):
            observations.append(
                Observation(
                    f"market.sell_{size}_terminal_price",
                    "binance",
                    symbol,
                    0.998,
                    symbol[-4:],
                    observed_at,
                    collected_at,
                    metadata={"fully_fillable": True},
                )
            )
        if symbol == "USD1USDC":
            self._active = None
        return observations


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_text(self, content: str) -> None:
        self.messages.append(content)


@pytest_asyncio.fixture
def fake_market_collector() -> FakeMarketCollector:
    return FakeMarketCollector()


@pytest_asyncio.fixture
def fake_notifier() -> FakeNotifier:
    return FakeNotifier()
