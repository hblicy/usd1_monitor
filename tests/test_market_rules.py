from datetime import UTC, datetime, timedelta

import pytest

from usd1_monitor.engine.market_rules import MarketSnapshot, evaluate_market
from usd1_monitor.engine.state import StateEngine
from usd1_monitor.models import RiskLevel, RuleEvaluation


START = datetime(2026, 9, 7, 4, 0, tzinfo=UTC)


def snapshot(
    at: datetime,
    price: float,
    terminal: float = 0.998,
    fillable: bool = True,
    trading: bool = True,
    exit_capacity: dict | None = None,
) -> MarketSnapshot:
    return MarketSnapshot(
        observed_at=at,
        mids={"USD1USDT": price, "USD1USDC": price},
        trading={"USD1USDT": trading, "USD1USDC": trading},
        one_million_terminal={"USD1USDT": terminal, "USD1USDC": terminal},
        one_million_fillable={"USD1USDT": fillable, "USD1USDC": fillable},
        exit_capacity=exit_capacity or {},
    )


def minute_history(minutes: int, price: float) -> list[MarketSnapshot]:
    return [
        snapshot(START + timedelta(minutes=minute), price)
        for minute in range(minutes + 1)
    ]


def test_price_becomes_yellow_only_after_fifteen_valid_minutes() -> None:
    assert evaluate_market(minute_history(14, 0.996)).price_level is RiskLevel.GREEN
    assert evaluate_market(minute_history(15, 0.996)).price_level is RiskLevel.YELLOW


def test_price_becomes_red_after_five_minutes_below_0995() -> None:
    assert evaluate_market(minute_history(5, 0.994)).price_level is RiskLevel.RED


def test_severe_depeg_becomes_red_only_after_one_hour_below_099() -> None:
    assert evaluate_market(minute_history(59, 0.989)).severe_level is RiskLevel.GREEN
    result = evaluate_market(minute_history(60, 0.989))
    assert result.severe_level is RiskLevel.RED
    assert result.price_level is RiskLevel.RED


def test_market_evidence_contains_all_exit_sizes_and_source_url() -> None:
    capacity = {
        "USD1USDT": {
            "1000000": {"terminal_price": 0.998, "fully_fillable": True},
            "5000000": {"terminal_price": 0.997, "fully_fillable": True},
            "20000000": {"terminal_price": 0.0, "fully_fillable": False},
        }
    }

    result = evaluate_market([snapshot(START, 1.0, exit_capacity=capacity)])

    assert result.evidence["market.liquidity"]["exit_capacity"] == capacity
    assert result.evidence["market.price"]["source_urls"] == [
        "https://api.binance.com/api/v3/depth?symbol=USD1USDC&limit=1000",
        "https://api.binance.com/api/v3/depth?symbol=USD1USDT&limit=1000",
    ]
    assert result.evidence["market.trading"]["source_urls"] == [
        "https://api.binance.com/api/v3/exchangeInfo?symbol=USD1USDC",
        "https://api.binance.com/api/v3/exchangeInfo?symbol=USD1USDT",
    ]
    assert result.evidence["market.price.severe"]["severe_threshold"] == 0.99


def test_missing_observation_does_not_count_toward_duration() -> None:
    history = [snapshot(START, 0.996), snapshot(START + timedelta(minutes=20), 0.996)]

    assert evaluate_market(history, max_gap_seconds=90).price_level is RiskLevel.GREEN


def test_two_unfillable_books_are_red() -> None:
    history = [
        snapshot(START, 1.0, fillable=False),
        snapshot(START + timedelta(minutes=1), 1.0, fillable=False),
    ]

    assert evaluate_market(history).liquidity_level is RiskLevel.RED


def test_non_trading_pair_is_red_after_two_valid_observations() -> None:
    history = [
        snapshot(START, 1.0, trading=False),
        snapshot(START + timedelta(minutes=1), 1.0, trading=False),
    ]

    assert evaluate_market(history).trading_level is RiskLevel.RED


def test_price_recovers_only_after_fifteen_healthy_minutes() -> None:
    history = minute_history(5, 0.994)
    history.extend(
        snapshot(START + timedelta(minutes=minute), 0.999)
        for minute in range(6, 21)
    )
    assert evaluate_market(history).price_level is RiskLevel.RED

    history.append(snapshot(START + timedelta(minutes=21), 0.999))
    assert evaluate_market(history).price_level is RiskLevel.GREEN


def test_initial_red_state_does_not_downgrade_without_recovery() -> None:
    history = minute_history(15, 0.996)

    assert evaluate_market(
        history, initial_price_level=RiskLevel.RED
    ).price_level is RiskLevel.RED


def test_missing_pair_cannot_recover_price_or_liquidity() -> None:
    history = []
    for minute in range(16):
        history.append(
            MarketSnapshot(
                observed_at=START + timedelta(minutes=minute),
                mids={"USD1USDC": 1.0},
                trading={"USD1USDT": False, "USD1USDC": True},
                one_million_terminal={"USD1USDC": 1.0},
                one_million_fillable={"USD1USDC": True},
            )
        )

    result = evaluate_market(
        history,
        initial_price_level=RiskLevel.RED,
        initial_liquidity_level=RiskLevel.RED,
    )

    assert result.price_level is RiskLevel.RED
    assert result.liquidity_level is RiskLevel.RED


@pytest.mark.asyncio
async def test_state_engine_records_only_real_transitions_and_survives_reopen(
    tmp_path,
) -> None:
    from usd1_monitor.storage import Storage

    db_path = tmp_path / "monitor.db"
    storage = Storage(db_path)
    await storage.open()
    engine = StateEngine(storage)
    evidence = {"current": 0.996, "threshold": 0.997}

    first = await engine.apply(
        [RuleEvaluation("market.price", RiskLevel.YELLOW, evidence)], START
    )
    duplicate = await engine.apply(
        [RuleEvaluation("market.price", RiskLevel.YELLOW, evidence)],
        START + timedelta(minutes=1),
    )
    upgraded = await engine.apply(
        [RuleEvaluation("market.price", RiskLevel.RED, evidence)],
        START + timedelta(minutes=2),
    )
    downgraded = await engine.apply(
        [RuleEvaluation("market.price", RiskLevel.YELLOW, evidence)],
        START + timedelta(minutes=3),
    )
    recovered = await engine.apply(
        [RuleEvaluation("market.price", RiskLevel.GREEN, evidence)],
        START + timedelta(minutes=4),
    )

    assert [item.current for item in first + upgraded + downgraded + recovered] == [
        RiskLevel.YELLOW,
        RiskLevel.RED,
        RiskLevel.YELLOW,
        RiskLevel.GREEN,
    ]
    assert duplicate == []
    assert upgraded[0].first_triggered_at == START
    assert downgraded[0].previous is RiskLevel.RED
    await storage.close()

    reopened = Storage(db_path)
    await reopened.open()
    assert await StateEngine(reopened).apply(
        [RuleEvaluation("market.price", RiskLevel.GREEN, evidence)],
        START + timedelta(minutes=5),
    ) == []
    assert await reopened.count_alert_deliveries() == 4
    await reopened.close()


@pytest.mark.asyncio
async def test_state_engine_groups_same_cause_into_one_pending_alert(storage) -> None:
    transitions = await StateEngine(storage).apply(
        [
            RuleEvaluation(
                "market.price", RiskLevel.RED, {"current": 0.994}, "depeg"
            ),
            RuleEvaluation(
                "market.liquidity", RiskLevel.RED, {"current": 0.993}, "depeg"
            ),
        ],
        START,
    )

    assert len(transitions) == 2
    assert await storage.count_alert_deliveries() == 1
    pending = await storage.pending_alerts()
    assert "market.price" in pending[0].content
    assert "market.liquidity" in pending[0].content
