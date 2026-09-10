from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from usd1_monitor.dashboard_data import DashboardDataError, DashboardRepository
from usd1_monitor.models import Observation, RiskLevel


NOW = datetime(2026, 9, 10, 8, 0, tzinfo=UTC)
EXPECTED_METRIC_KEYS = {
    "price_usd1usdt",
    "price_usd1usdc",
    "exit_usd1usdt_1m",
    "exit_usd1usdc_1m",
    "reserves",
    "multichain_supply",
    "estimated_collateralization",
    "bridged_total",
    "locked_total",
    "bridge_delta",
    "supply_change_24h",
}


async def insert_observation(
    storage,
    metric: str,
    scope: str,
    value: float,
    *,
    observed_at: datetime = NOW,
    unit: str = "USD1",
    metadata: dict[str, object] | None = None,
) -> None:
    await storage.insert_observation(
        Observation(
            metric=metric,
            source="test",
            scope=scope,
            value=value,
            unit=unit,
            observed_at=observed_at,
            collected_at=observed_at + timedelta(seconds=2),
            quality="FACT",
            metadata=metadata or {},
        )
    )


@pytest.mark.asyncio
async def test_repository_rejects_missing_database_without_creating_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "missing.db"
    repository = DashboardRepository(path)

    with pytest.raises(DashboardDataError, match="does not exist"):
        await repository.open()

    assert path.exists() is False


@pytest.mark.asyncio
async def test_repository_connection_is_query_only(storage) -> None:
    repository = DashboardRepository(storage.path)
    await repository.open()
    try:
        with pytest.raises(aiosqlite.OperationalError, match="readonly"):
            await repository.connection.execute(
                "INSERT INTO risk_states VALUES ('x', 0, 'a', 'b')"
            )
    finally:
        await repository.close()


@pytest.mark.asyncio
async def test_snapshot_separates_business_and_health_states(storage) -> None:
    await storage.set_risk_state("market.price", RiskLevel.RED, NOW, NOW)
    await storage.set_risk_state("health.evm_bsc", RiskLevel.YELLOW, NOW, NOW)
    repository = DashboardRepository(storage.path)
    await repository.open()
    try:
        snapshot = await repository.snapshot(now=NOW)
    finally:
        await repository.close()

    assert snapshot["business"]["level"] == "RED"
    assert snapshot["health"]["level"] == "YELLOW"
    assert [item["rule_id"] for item in snapshot["business"]["items"]] == [
        "market.price"
    ]
    assert [item["rule_id"] for item in snapshot["health"]["items"]] == [
        "health.evm_bsc"
    ]


@pytest.mark.asyncio
async def test_snapshot_reports_unknown_when_no_states_exist(storage) -> None:
    repository = DashboardRepository(storage.path)
    await repository.open()
    try:
        snapshot = await repository.snapshot(now=NOW)
    finally:
        await repository.close()

    assert snapshot["business"] == {"level": "UNKNOWN", "items": []}
    assert snapshot["health"] == {"level": "UNKNOWN", "items": []}


@pytest.mark.asyncio
async def test_snapshot_uses_alert_text_and_falls_back_to_readable_rule_label(
    storage,
) -> None:
    await storage.set_risk_state("market.price", RiskLevel.RED, NOW, NOW)
    await storage.set_risk_state("por.age", RiskLevel.YELLOW, NOW, NOW)
    await storage.insert_pending_alert_uncommitted(
        f"rule:0:market.price:{NOW.isoformat()}",
        "hash",
        "USD1 价格低于风险阈值",
        NOW,
    )
    await storage.connection.commit()
    repository = DashboardRepository(storage.path)
    await repository.open()
    try:
        snapshot = await repository.snapshot(now=NOW)
    finally:
        await repository.close()

    items = {item["rule_id"]: item for item in snapshot["business"]["items"]}
    assert items["market.price"]["summary"] == "USD1 价格低于风险阈值"
    assert items["por.age"]["summary"] == "USD1 储备数据长时间没有更新"


@pytest.mark.asyncio
async def test_snapshot_returns_latest_core_metrics(storage) -> None:
    metric_rows = (
        ("market.mid_price", "USD1USDT", 0.999, "USDT", {}),
        ("market.mid_price", "USD1USDC", 1.001, "USDC", {}),
        (
            "market.sell_1000000_terminal_price",
            "USD1USDT",
            0.998,
            "USDT",
            {"fully_fillable": True, "ignored": "secret"},
        ),
        (
            "market.sell_1000000_terminal_price",
            "USD1USDC",
            0.997,
            "USDC",
            {"fully_fillable": False},
        ),
        ("por.reserves", "ethereum", 4_300_000_000, "USD", {}),
        ("supply.multichain_total", "global", 4_200_000_000, "USD1", {}),
        ("supply.estimated_collateralization", "global", 1.0238, "ratio", {}),
        ("supply.bridged_total", "global", 100_000_000, "USD1", {}),
        ("bridge.locked_total", "global", 99_500_000, "USD1", {}),
        ("bridge.issuance_delta", "global", 500_000, "USD1", {}),
    )
    for metric, scope, value, unit, metadata in metric_rows:
        await insert_observation(
            storage,
            metric,
            scope,
            value - 1,
            observed_at=NOW - timedelta(minutes=5),
            unit=unit,
            metadata=metadata,
        )
        await insert_observation(
            storage,
            metric,
            scope,
            value,
            unit=unit,
            metadata=metadata,
        )
    await insert_observation(
        storage,
        "supply.multichain_total",
        "global",
        4_000_000_000,
        observed_at=NOW - timedelta(hours=24, minutes=30),
    )
    repository = DashboardRepository(storage.path)
    await repository.open()
    try:
        metrics = (await repository.snapshot(now=NOW))["metrics"]
    finally:
        await repository.close()

    assert set(metrics) == EXPECTED_METRIC_KEYS
    assert metrics["price_usd1usdt"]["value"] == 0.999
    assert metrics["price_usd1usdt"]["unit"] == "USDT"
    assert metrics["price_usd1usdt"]["quality"] == "FACT"
    assert metrics["price_usd1usdt"]["observed_at"].endswith("+08:00")
    assert metrics["price_usd1usdt"]["collected_at"].endswith("+08:00")
    assert metrics["exit_usd1usdt_1m"]["fully_fillable"] is True
    assert "ignored" not in metrics["exit_usd1usdt_1m"]
    assert metrics["supply_change_24h"]["value"] == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_snapshot_uses_none_for_missing_core_metrics(storage) -> None:
    repository = DashboardRepository(storage.path)
    await repository.open()
    try:
        metrics = (await repository.snapshot(now=NOW))["metrics"]
    finally:
        await repository.close()

    assert set(metrics) == EXPECTED_METRIC_KEYS
    assert all(value is None for value in metrics.values())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("baseline_age", "current", "baseline", "expected"),
    [
        (timedelta(hours=24, minutes=30), 105.0, 100.0, 5.0),
        (timedelta(hours=25, minutes=16), 105.0, 100.0, None),
        (timedelta(hours=24, minutes=30), 0.0, 100.0, None),
        (timedelta(hours=24, minutes=30), 105.0, 0.0, None),
    ],
)
async def test_supply_change_24h_requires_recent_positive_baseline(
    storage,
    baseline_age: timedelta,
    current: float,
    baseline: float,
    expected: float | None,
) -> None:
    await insert_observation(
        storage,
        "supply.multichain_total",
        "global",
        baseline,
        observed_at=NOW - baseline_age,
    )
    await insert_observation(
        storage,
        "supply.multichain_total",
        "global",
        current,
    )
    repository = DashboardRepository(storage.path)
    await repository.open()
    try:
        change = (await repository.snapshot(now=NOW))["metrics"][
            "supply_change_24h"
        ]
    finally:
        await repository.close()

    if expected is None:
        assert change is None
    else:
        assert change["value"] == pytest.approx(expected)
        assert change["unit"] == "percent"
        assert change["quality"] == "CALCULATED"
