from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest

from usd1_monitor.dashboard_data import DashboardDataError, DashboardRepository
from usd1_monitor.models import RiskLevel


NOW = datetime(2026, 9, 10, 8, 0, tzinfo=UTC)


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
