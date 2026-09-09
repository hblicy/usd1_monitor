from datetime import UTC, datetime, timedelta

import pytest

from usd1_monitor.models import Observation, RiskLevel
from usd1_monitor.config import MarketConfig
from usd1_monitor.engine.state import StateEngine
from usd1_monitor.scheduler import MarketMonitor, Usd1Monitor


START = datetime(2026, 9, 7, 4, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_market_monitor_persists_one_yellow_transition(
    fake_market_collector, storage, fake_notifier
) -> None:
    for minute in range(16):
        fake_market_collector.queue_price(
            0.996, START + timedelta(minutes=minute)
        )
    monitor = MarketMonitor(fake_market_collector, storage, fake_notifier)

    for _ in range(16):
        result = await monitor.check_once()

    assert result.success is True
    assert len(fake_notifier.messages) == 1
    assert fake_notifier.messages[0].startswith("🟡 USD1 注意")
    assert "USD1 价格低于预警线" in fake_notifier.messages[0]
    assert (await storage.get_risk_state("market.price")).level.name == "YELLOW"


@pytest.mark.asyncio
async def test_check_can_collect_without_delivering_alerts(
    fake_market_collector, storage, fake_notifier
) -> None:
    for minute in range(16):
        fake_market_collector.queue_price(
            0.996, START + timedelta(minutes=minute)
        )
    monitor = MarketMonitor(fake_market_collector, storage, fake_notifier)

    for _ in range(16):
        await monitor.check_once(deliver=False)

    assert fake_notifier.messages == []


@pytest.mark.asyncio
async def test_startup_notice_is_sent_once_and_not_from_check(
    fake_market_collector, storage, fake_notifier
) -> None:
    fake_market_collector.queue_price(1.0, START)
    monitor = MarketMonitor(fake_market_collector, storage, fake_notifier)

    await monitor.check_once()
    assert len(fake_notifier.messages) == 0
    await monitor.send_startup_once()
    await monitor.send_startup_once()

    assert len(fake_notifier.messages) == 1
    message = fake_notifier.messages[0]
    assert message.startswith("🟢 USD1 监控已启动")
    assert "正在监控：\n价格、流动性、交易状态" in message
    assert "暂未覆盖：" in message
    assert "社交媒体情绪" in message
    for hidden in ("enabled_collectors", "NOT_MONITORED", "binance_market"):
        assert hidden not in message


@pytest.mark.asyncio
async def test_restart_does_not_clear_yellow_before_full_recovery_window(
    fake_market_collector, storage, fake_notifier
) -> None:
    for minute in range(16):
        fake_market_collector.queue_price(
            0.996, START + timedelta(minutes=minute)
        )
    first_monitor = MarketMonitor(fake_market_collector, storage, fake_notifier)
    for _ in range(16):
        await first_monitor.check_once(deliver=False)
    assert (await storage.get_risk_state("market.price")).level.name == "YELLOW"

    restarted = MarketMonitor(fake_market_collector, storage, fake_notifier)
    fake_market_collector.queue_price(0.999, START + timedelta(minutes=16))
    await restarted.check_once(deliver=False)

    assert (await storage.get_risk_state("market.price")).level.name == "YELLOW"

    for minute in range(17, 32):
        fake_market_collector.queue_price(0.999, START + timedelta(minutes=minute))
        await restarted.check_once(deliver=False)
    assert (await storage.get_risk_state("market.price")).level.name == "GREEN"


class MissingUsdtCollector:
    async def collect_symbol(self, symbol: str, collected_at: datetime):
        status = Observation(
            "market.symbol_trading", "binance", symbol,
            0.0 if symbol == "USD1USDT" else 1.0,
            "bool", collected_at, collected_at,
            metadata={"status": "ABSENT" if symbol == "USD1USDT" else "TRADING"},
        )
        if symbol == "USD1USDT":
            return [status]
        values = [
            status,
            Observation(
                "market.mid_price", "binance", symbol, 1.0, "USDC",
                collected_at, collected_at,
            ),
        ]
        for size in (1_000_000, 5_000_000, 20_000_000):
            values.append(
                Observation(
                    f"market.sell_{size}_terminal_price", "binance", symbol,
                    0.999, "USDC", collected_at, collected_at,
                    metadata={"fully_fillable": True},
                )
            )
        return values


@pytest.mark.asyncio
async def test_missing_symbol_twice_sets_trading_red(storage) -> None:
    monitor = MarketMonitor(MissingUsdtCollector(), storage, None)

    assert (await monitor.check_once(deliver=False)).success is True
    assert (await monitor.check_once(deliver=False)).success is True

    state = await storage.get_risk_state("market.trading")
    assert state is not None
    assert state.level is RiskLevel.RED


@pytest.mark.asyncio
async def test_restart_preserves_price_trigger_elapsed_time(
    fake_market_collector, storage, fake_notifier
) -> None:
    first = MarketMonitor(fake_market_collector, storage, fake_notifier)
    for minute in range(15):
        fake_market_collector.queue_price(
            0.996, START + timedelta(minutes=minute)
        )
        await first.check_once(deliver=False)

    restarted = MarketMonitor(fake_market_collector, storage, fake_notifier)
    fake_market_collector.queue_price(0.996, START + timedelta(minutes=15))
    await restarted.check_once(deliver=False)

    state = await storage.get_risk_state("market.price")
    assert state is not None
    assert state.level is RiskLevel.YELLOW


@pytest.mark.asyncio
async def test_market_observations_roll_back_when_state_update_fails(
    fake_market_collector, storage, monkeypatch
) -> None:
    fake_market_collector.queue_price(0.996, START)
    monitor = MarketMonitor(fake_market_collector, storage, None)

    async def fail_state(*args, **kwargs):
        raise RuntimeError("state write failed")

    monkeypatch.setattr(StateEngine, "apply_uncommitted", fail_state)

    with pytest.raises(RuntimeError, match="state write failed"):
        await monitor.check_once(deliver=False)

    assert await storage.latest_observations("market.mid_price", limit=10) == []


@pytest.mark.asyncio
async def test_market_monitor_bounds_in_memory_history(
    fake_market_collector, storage
) -> None:
    config = MarketConfig(
        interval_seconds=10,
        yellow_seconds=120,
        red_seconds=60,
        severe_seconds=120,
        recovery_seconds=120,
    )
    monitor = MarketMonitor(fake_market_collector, storage, None, config)
    for step in range(100):
        fake_market_collector.queue_price(
            1.0, START + timedelta(seconds=step * 10)
        )
        await monitor.check_once(deliver=False)

    assert len(monitor._history) <= 22


@pytest.mark.asyncio
async def test_market_monitor_emits_separate_severe_depeg_alert(
    fake_market_collector, storage
) -> None:
    monitor = MarketMonitor(
        fake_market_collector,
        storage,
        None,
        MarketConfig(
            interval_seconds=10,
            red_seconds=10,
            severe_seconds=20,
        ),
    )
    for step in range(3):
        fake_market_collector.queue_price(
            0.989, START + timedelta(seconds=step * 10)
        )
        await monitor.check_once(deliver=False)

    state = await storage.get_risk_state("market.price.severe")
    pending = await storage.pending_alerts()
    assert state is not None
    assert state.level is RiskLevel.RED
    assert any("USD1 价格持续严重偏离 1 美元" in item.content for item in pending)


@pytest.mark.asyncio
async def test_component_timeout_rolls_back_open_transaction(
    fake_market_collector, storage, monkeypatch
) -> None:
    fake_market_collector.queue_price(1.0, START)

    original_apply = StateEngine.apply_uncommitted

    async def wait_forever(self, evaluations, now):
        values = list(evaluations)
        if any(item.rule_id.startswith("market.") for item in values):
            await __import__("asyncio").Event().wait()
        return await original_apply(self, values, now)

    monkeypatch.setattr(StateEngine, "apply_uncommitted", wait_forever)
    composite = Usd1Monitor(
        MarketMonitor(fake_market_collector, storage, None),
        [],
        storage,
        None,
        check_timeout_seconds=0.01,
    )

    result = await composite.check_once(deliver=False)

    assert result.success is False
    assert storage.connection.in_transaction is False
    assert await storage.latest_observations("market.mid_price", limit=1) == []


@pytest.mark.asyncio
async def test_same_market_cycle_groups_price_and_liquidity_alerts(storage) -> None:
    class BadMarketCollector:
        cycle = 0

        async def collect_symbol(self, symbol: str, collected_at: datetime):
            observed_at = START + timedelta(seconds=self.cycle * 10)
            values = [
                Observation(
                    "market.symbol_trading", "binance", symbol, 1,
                    "bool", observed_at, collected_at,
                    metadata={"status": "TRADING"},
                ),
                Observation(
                    "market.mid_price", "binance", symbol, 0.994,
                    symbol[-4:], observed_at, collected_at,
                ),
            ]
            for size in (1_000_000, 5_000_000, 20_000_000):
                values.append(
                    Observation(
                        f"market.sell_{size}_terminal_price", "binance", symbol,
                        0.994, symbol[-4:], observed_at, collected_at,
                        metadata={"fully_fillable": True},
                    )
                )
            if symbol == "USD1USDC":
                self.cycle += 1
            return values

    monitor = MarketMonitor(
        BadMarketCollector(),
        storage,
        None,
        MarketConfig(interval_seconds=10, red_seconds=10),
    )

    await monitor.check_once(deliver=False)
    await monitor.check_once(deliver=False)

    pending = await storage.pending_alerts()
    assert len(pending) == 1
    assert "USD1 价格低于预警线" in pending[0].content
    assert "USD1 市场流动性不足" in pending[0].content
    assert "market.price" not in pending[0].content
    assert "market.liquidity" not in pending[0].content


@pytest.mark.asyncio
async def test_market_alert_contains_all_configured_exit_sizes(storage) -> None:
    class MultiSizeCollector:
        cycle = 0

        async def collect_symbol(self, symbol: str, collected_at: datetime):
            observed_at = START + timedelta(seconds=self.cycle * 10)
            values = [
                Observation(
                    "market.symbol_trading", "binance", symbol, 1, "bool",
                    observed_at, collected_at, metadata={"status": "TRADING"},
                ),
                Observation(
                    "market.mid_price", "binance", symbol, 1.0, symbol[-4:],
                    observed_at, collected_at,
                ),
            ]
            for size, terminal, fillable in (
                (1_000_000, 0.994, True),
                (5_000_000, 0.991, True),
                (20_000_000, 0.0, False),
            ):
                values.append(
                    Observation(
                        f"market.sell_{size}_terminal_price", "binance", symbol,
                        terminal, symbol[-4:], observed_at, collected_at,
                        metadata={"fully_fillable": fillable},
                    )
                )
            if symbol == "USD1USDC":
                self.cycle += 1
            return values

    monitor = MarketMonitor(
        MultiSizeCollector(), storage, None,
        MarketConfig(interval_seconds=10, red_seconds=10),
    )

    await monitor.check_once(deliver=False)
    await monitor.check_once(deliver=False)

    pending = await storage.pending_alerts()
    assert len(pending) == 1
    assert "500 万 USD1" in pending[0].content
    assert "2000 万 USD1" in pending[0].content
