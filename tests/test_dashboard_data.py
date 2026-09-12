import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from usd1_monitor.dashboard_data import (
    DashboardDataError,
    DashboardRepository,
    sanitize_dashboard_text,
)
from usd1_monitor.models import Announcement, ChainEvent, Observation, RiskLevel


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
    quality: str = "FACT",
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
            quality=quality,
            metadata=metadata or {},
        )
    )


async def seed_asset_pillars(
    storage,
    *,
    concentration_at: datetime = NOW,
    reserves_at: datetime = NOW,
    supply_at: datetime = NOW,
    redemption_at: datetime = NOW,
    reserves_quality: str = "FACT",
    supply_quality: str = "FACT",
) -> None:
    await insert_observation(
        storage,
        "custody.binance_share_lower_bound",
        "global",
        0.40,
        observed_at=concentration_at,
        unit="ratio",
        metadata={"max_age_seconds": 1200},
    )
    await insert_observation(
        storage,
        "por.reserves",
        "ethereum",
        4_200_000_000,
        observed_at=reserves_at,
        unit="USD",
        quality=reserves_quality,
    )
    await insert_observation(
        storage,
        "supply.multichain_total",
        "global",
        4_100_000_000,
        observed_at=supply_at,
        quality=supply_quality,
    )
    await insert_observation(
        storage,
        "redemption.channel_status",
        "global",
        0,
        observed_at=redemption_at,
        unit="risk_level",
        metadata={"max_age_seconds": 900},
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
async def test_snapshot_serializes_concurrent_reads_on_one_connection(storage) -> None:
    repository = DashboardRepository(storage.path)
    await repository.open()
    try:
        snapshots = await asyncio.gather(
            *(repository.snapshot(now=NOW) for _ in range(10))
        )
    finally:
        await repository.close()

    assert len(snapshots) == 10
    assert all(snapshot["generated_at"].endswith("+08:00") for snapshot in snapshots)


@pytest.mark.asyncio
async def test_snapshot_rolls_back_unexpected_error_before_next_read(
    storage, monkeypatch
) -> None:
    repository = DashboardRepository(storage.path)
    await repository.open()
    original_metrics = repository._metrics
    failure = RuntimeError("unexpected metrics failure")

    async def fail_metrics_once(now: datetime):
        monkeypatch.setattr(repository, "_metrics", original_metrics)
        raise failure

    monkeypatch.setattr(repository, "_metrics", fail_metrics_once)
    try:
        with pytest.raises(RuntimeError) as caught:
            await repository.snapshot(now=NOW)
        recovered = await repository.snapshot(now=NOW)
    finally:
        await repository.close()

    assert caught.value is failure
    assert recovered["generated_at"].endswith("+08:00")


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

    assert snapshot["business"] == {
        "level": "UNKNOWN",
        "items": [],
        "missing_pillars": [
            {"name": "concentration", "reason": "custody_unavailable"},
            {"name": "coverage", "reason": "coverage_unavailable"},
            {"name": "redemption", "reason": "redemption_unavailable"},
        ],
    }
    assert snapshot["health"]["level"] == "UNKNOWN"
    assert snapshot["health"]["items"] == []


@pytest.mark.asyncio
async def test_dashboard_business_is_unknown_when_por_is_stale(storage) -> None:
    await storage.set_risk_state("market.price", RiskLevel.GREEN, NOW, NOW)
    await seed_asset_pillars(storage, reserves_at=NOW - timedelta(minutes=30))
    await insert_observation(
        storage,
        "supply.estimated_collateralization",
        "global",
        102.44,
        observed_at=NOW,
        unit="percent",
        quality="ESTIMATED",
    )
    repository = DashboardRepository(storage.path)
    await repository.open()
    try:
        snapshot = await repository.snapshot(now=NOW)
    finally:
        await repository.close()

    assert snapshot["business"]["level"] == "UNKNOWN"
    assert snapshot["business"]["missing_pillars"] == [
        {"name": "coverage", "reason": "coverage_unavailable"}
    ]
    assert snapshot["metrics"]["reserves"]["value"] == 4_200_000_000
    assert snapshot["metrics"]["estimated_collateralization"] is None


@pytest.mark.asyncio
async def test_dashboard_business_is_green_when_all_pillars_are_ready(storage) -> None:
    await storage.set_risk_state("market.price", RiskLevel.GREEN, NOW, NOW)
    await seed_asset_pillars(storage)
    repository = DashboardRepository(storage.path)
    await repository.open()
    try:
        business = (await repository.snapshot(now=NOW))["business"]
    finally:
        await repository.close()

    assert business["level"] == "GREEN"
    assert business["missing_pillars"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("max_age_seconds", "reserve_age_seconds", "expected_level"),
    [(900, 900, "UNKNOWN"), (3600, 1800, "GREEN")],
)
async def test_dashboard_uses_configured_por_coverage_freshness(
    storage,
    max_age_seconds: int,
    reserve_age_seconds: int,
    expected_level: str,
) -> None:
    await storage.set_risk_state("market.price", RiskLevel.GREEN, NOW, NOW)
    await seed_asset_pillars(
        storage,
        reserves_at=NOW - timedelta(seconds=reserve_age_seconds),
    )
    repository = DashboardRepository(
        storage.path,
        coverage_max_age_seconds=max_age_seconds,
    )
    await repository.open()
    try:
        business = (await repository.snapshot(now=NOW))["business"]
    finally:
        await repository.close()

    assert business["level"] == expected_level


@pytest.mark.parametrize("max_age_seconds", [0, True])
def test_dashboard_rejects_invalid_por_coverage_freshness(
    storage,
    max_age_seconds: object,
) -> None:
    with pytest.raises(ValueError, match="coverage_max_age_seconds"):
        DashboardRepository(
            storage.path,
            coverage_max_age_seconds=max_age_seconds,
        )


@pytest.mark.asyncio
async def test_known_yellow_precedes_missing_pillar(storage) -> None:
    await storage.set_risk_state("market.price", RiskLevel.YELLOW, NOW, NOW)
    repository = DashboardRepository(storage.path)
    await repository.open()
    try:
        business = (await repository.snapshot(now=NOW))["business"]
    finally:
        await repository.close()

    assert business["level"] == "YELLOW"
    assert len(business["missing_pillars"]) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("supply_age", "supply_quality", "expected_level"),
    [
        (4500, "FACT", "GREEN"),
        (4501, "FACT", "UNKNOWN"),
        (0, "ESTIMATED", "UNKNOWN"),
    ],
)
async def test_dashboard_coverage_reuses_supply_freshness_and_quality_rules(
    storage,
    supply_age: int,
    supply_quality: str,
    expected_level: str,
) -> None:
    await storage.set_risk_state("market.price", RiskLevel.GREEN, NOW, NOW)
    await seed_asset_pillars(
        storage,
        supply_at=NOW - timedelta(seconds=supply_age),
        supply_quality=supply_quality,
    )
    repository = DashboardRepository(storage.path)
    await repository.open()
    try:
        business = (await repository.snapshot(now=NOW))["business"]
    finally:
        await repository.close()

    assert business["level"] == expected_level


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pillar", "age_seconds"),
    [("concentration", 1200), ("redemption", 900)],
)
async def test_dashboard_metadata_freshness_boundary_is_unavailable(
    storage,
    pillar: str,
    age_seconds: int,
) -> None:
    times = {
        "concentration_at": NOW,
        "redemption_at": NOW,
    }
    times[f"{pillar}_at"] = NOW - timedelta(seconds=age_seconds)
    await storage.set_risk_state("market.price", RiskLevel.GREEN, NOW, NOW)
    await seed_asset_pillars(storage, **times)
    repository = DashboardRepository(storage.path)
    await repository.open()
    try:
        business = (await repository.snapshot(now=NOW))["business"]
    finally:
        await repository.close()

    assert business["level"] == "UNKNOWN"
    assert business["missing_pillars"][0]["name"] == pillar


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "observed_at", [NOW.replace(tzinfo=None), NOW + timedelta(seconds=1)]
)
async def test_dashboard_rejects_invalid_critical_pillar_time(
    storage,
    observed_at: datetime,
) -> None:
    await storage.set_risk_state("market.price", RiskLevel.GREEN, NOW, NOW)
    await seed_asset_pillars(storage, reserves_at=observed_at)
    repository = DashboardRepository(storage.path)
    await repository.open()
    try:
        with pytest.raises(DashboardDataError, match="database snapshot cannot be read"):
            await repository.snapshot(now=NOW)
    finally:
        await repository.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [{}, {"max_age_seconds": 0}, {"max_age_seconds": True}],
)
async def test_dashboard_requires_positive_integer_pillar_freshness_metadata(
    storage,
    metadata: dict[str, object],
) -> None:
    await storage.set_risk_state("market.price", RiskLevel.GREEN, NOW, NOW)
    await seed_asset_pillars(storage)
    await insert_observation(
        storage,
        "custody.binance_share_lower_bound",
        "global",
        0.40,
        unit="ratio",
        metadata=metadata,
    )
    repository = DashboardRepository(storage.path)
    await repository.open()
    try:
        business = (await repository.snapshot(now=NOW))["business"]
    finally:
        await repository.close()

    assert business["level"] == "UNKNOWN"
    assert business["missing_pillars"][0]["name"] == "concentration"


@pytest.mark.asyncio
async def test_snapshot_marks_monitor_red_at_collector_stale_boundary(storage) -> None:
    await storage.record_collector_success("older_success", NOW - timedelta(hours=1))
    await storage.record_collector_failure("latest_failure", NOW, "timeout")
    repository = DashboardRepository(storage.path)
    await repository.open()
    try:
        fresh = await repository.snapshot(now=NOW + timedelta(minutes=14, seconds=59))
        stale = await repository.snapshot(now=NOW + timedelta(minutes=15))
    finally:
        await repository.close()

    assert fresh["health"]["level"] == "UNKNOWN"
    assert fresh["health"]["items"] == []
    assert stale["generated_at"] == "2026-09-10T16:15:00+08:00"
    assert stale["health"]["level"] == "RED"
    assert stale["health"]["items"] == [
        {
            "rule_id": "health.monitor_stale",
            "level": "RED",
            "summary": "监控数据已经停止更新",
            "first_triggered_at": "2026-09-10T16:15:00+08:00",
            "changed_at": "2026-09-10T16:15:00+08:00",
        }
    ]


@pytest.mark.asyncio
async def test_snapshot_stale_monitor_overrides_persisted_green_health(storage) -> None:
    await storage.record_collector_success("market", NOW)
    await storage.set_risk_state("health.por", RiskLevel.GREEN, NOW, NOW)
    repository = DashboardRepository(storage.path)
    await repository.open()
    try:
        health = (await repository.snapshot(now=NOW + timedelta(minutes=15)))[
            "health"
        ]
    finally:
        await repository.close()

    assert health["level"] == "RED"
    assert [item["rule_id"] for item in health["items"]] == [
        "health.monitor_stale"
    ]


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

    business_items = {
        item["rule_id"]: item for item in snapshot["business"]["items"]
    }
    health_items = {item["rule_id"]: item for item in snapshot["health"]["items"]}
    assert business_items["market.price"]["summary"] == "USD1 价格低于风险阈值"
    assert "por.age" not in business_items
    assert health_items["por.age"]["summary"] == "USD1 储备数据长时间没有更新"


@pytest.mark.asyncio
async def test_snapshot_replaces_legacy_technical_alert_with_plain_label(storage) -> None:
    await storage.set_risk_state("por.age", RiskLevel.RED, NOW, NOW)
    await storage.set_risk_state(
        "information.wlfi.attestation", RiskLevel.YELLOW, NOW, NOW
    )
    await storage.insert_pending_alert_uncommitted(
        f"rule:0:por.age:{NOW.isoformat()}",
        "legacy",
        "USD1 风险状态：RED\n- por.age: 当前值=48190; 阈值=3600",
        NOW,
    )
    await storage.connection.commit()
    repository = DashboardRepository(storage.path)
    await repository.open()
    try:
        snapshot = await repository.snapshot(now=NOW)
    finally:
        await repository.close()

    business_items = {
        item["rule_id"]: item for item in snapshot["business"]["items"]
    }
    health_items = {item["rule_id"]: item for item in snapshot["health"]["items"]}
    assert "por.age" not in business_items
    assert health_items["por.age"]["summary"] == "USD1 储备数据长时间没有更新"
    assert business_items["information.wlfi.attestation"]["summary"] == (
        "官方信息出现需要关注的变化"
    )


@pytest.mark.asyncio
async def test_snapshot_matches_rule_ids_literally_in_alert_keys(storage) -> None:
    await storage.set_risk_state("health.evm_bsc", RiskLevel.YELLOW, NOW, NOW)
    await storage.insert_pending_alert_uncommitted(
        f"rule:0:health.evmXbsc:{NOW.isoformat()}",
        "wrong-rule",
        "不应匹配其他规则",
        NOW,
    )
    await storage.connection.commit()
    repository = DashboardRepository(storage.path)
    await repository.open()
    try:
        snapshot = await repository.snapshot(now=NOW)
    finally:
        await repository.close()

    assert snapshot["health"]["items"][0]["summary"] == (
        "BNB Chain 链上数据获取异常"
    )


@pytest.mark.asyncio
async def test_recent_alert_replaces_legacy_technical_content(storage) -> None:
    rule_id = "event.information.wlfi./usd1-token/attestation"
    await storage.insert_pending_alert_uncommitted(
        f"rule:0:{rule_id}:{NOW.isoformat()}",
        "legacy-recent",
        (
            "USD1 风险状态：YELLOW\n"
            f"- {rule_id}: 当前值=CHANGED; "
            "阈值=official risk keyword"
        ),
        NOW,
    )
    await storage.connection.commit()
    repository = DashboardRepository(storage.path)
    await repository.open()
    try:
        recent = (await repository.snapshot(now=NOW))["recent"]
    finally:
        await repository.close()

    assert recent["alerts"][0]["content"] == "官方信息出现需要关注的变化"


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


@pytest.mark.asyncio
async def test_supply_change_24h_rejects_stale_current_supply(storage) -> None:
    await insert_observation(
        storage,
        "supply.multichain_total",
        "global",
        100.0,
        observed_at=NOW - timedelta(hours=24, minutes=30),
    )
    await insert_observation(
        storage,
        "supply.multichain_total",
        "global",
        105.0,
        observed_at=NOW - timedelta(seconds=4501),
    )
    repository = DashboardRepository(storage.path)
    await repository.open()
    try:
        change = (await repository.snapshot(now=NOW))["metrics"][
            "supply_change_24h"
        ]
    finally:
        await repository.close()

    assert change is None


@pytest.mark.asyncio
async def test_snapshot_sanitizes_collector_errors_and_exposes_health(storage) -> None:
    error = (
        "POST https://rpc.example/v3/secret-key?token=hidden failed status=429 "
        f"database={storage.path.resolve()}"
    )
    await storage.record_collector_success("evm_bsc", NOW - timedelta(hours=1))
    for minute in range(3):
        await storage.record_collector_failure(
            "evm_bsc", NOW + timedelta(minutes=minute), error
        )
    repository = DashboardRepository(storage.path)
    await repository.open()
    try:
        health = (await repository.snapshot(now=NOW))["health"]
    finally:
        await repository.close()

    assert len(health["collectors"]) == 1
    collector = health["collectors"][0]
    assert collector["collector_id"] == "evm_bsc"
    assert collector["consecutive_failures"] == 3
    assert collector["last_success_at"].endswith("+08:00")
    assert collector["last_failure_at"].endswith("+08:00")
    assert "https://rpc.example" in collector["last_error"]
    assert "status=429" in collector["last_error"]
    assert "secret-key" not in collector["last_error"]
    assert "token=hidden" not in collector["last_error"]
    assert str(storage.path.resolve()) not in collector["last_error"]


@pytest.mark.asyncio
async def test_snapshot_ignores_legacy_supply_health_rows(storage) -> None:
    for collector_id in ("supply_ethereum", "supply_bsc"):
        await storage.record_collector_failure(collector_id, NOW, "old 429")
        await storage.set_risk_state(
            f"health.{collector_id}", RiskLevel.RED, NOW, NOW
        )
    await storage.record_collector_success("supply_native_ethereum", NOW)
    await storage.record_collector_success("supply_native_bsc", NOW)
    repository = DashboardRepository(storage.path)
    await repository.open()
    try:
        health = (await repository.snapshot(now=NOW))["health"]
    finally:
        await repository.close()

    assert health["level"] == "UNKNOWN"
    assert health["items"] == []
    assert {item["collector_id"] for item in health["collectors"]} == {
        "supply_native_ethereum",
        "supply_native_bsc",
    }


@pytest.mark.asyncio
async def test_snapshot_ignores_legacy_supply_recent_alerts(storage) -> None:
    await storage.insert_pending_alert_uncommitted(
        f"rule:2:market.price:{NOW.isoformat()}",
        "current-alert",
        "当前有效告警",
        NOW,
    )
    for index, collector_id in enumerate(
        ("supply_ethereum", "supply_bsc"), start=1
    ):
        created_at = NOW + timedelta(minutes=index)
        await storage.insert_pending_alert_uncommitted(
            f"rule:2:health.{collector_id}:{created_at.isoformat()}",
            f"legacy-{collector_id}",
            f"旧版 {collector_id} 429",
            created_at,
        )
    await storage.connection.commit()
    repository = DashboardRepository(storage.path)
    await repository.open()
    try:
        alerts = (await repository.snapshot(now=NOW))["recent"]["alerts"]
    finally:
        await repository.close()

    assert [item["content"] for item in alerts] == ["当前有效告警"]


def test_dashboard_text_keeps_source_path_but_removes_url_secrets() -> None:
    value = (
        "来源 https://docs.example/report?id=123&token=hidden#section，请核对"
    )

    sanitized = sanitize_dashboard_text(value)

    assert "https://docs.example/report" in sanitized
    assert "?" not in sanitized
    assert "#section" not in sanitized
    assert "token=hidden" not in sanitized


@pytest.mark.asyncio
async def test_snapshot_returns_bounded_deduplicated_recent_items(storage) -> None:
    for index in range(12):
        created_at = NOW + timedelta(minutes=index)
        key = f"rule:{index}:market.price:{created_at.isoformat()}"
        await storage.insert_pending_alert_uncommitted(
            key,
            f"hash-{index}",
            f"告警 {index}",
            created_at,
        )
    chunk_time = NOW + timedelta(minutes=20)
    for part, content in ((1, "第一部分"), (2, "第二部分")):
        await storage.insert_pending_alert_uncommitted(
            f"cause:{chunk_time.isoformat()}:part:{part:03d}",
            f"chunk-{part}",
            content,
            chunk_time,
        )
    cancelled_time = NOW + timedelta(minutes=21)
    await storage.insert_pending_alert_uncommitted(
        f"cancelled:{cancelled_time.isoformat()}",
        "cancelled",
        "不应显示",
        cancelled_time,
    )
    await storage.connection.execute(
        "UPDATE alert_deliveries SET status = 'CANCELLED' WHERE payload_hash = 'cancelled'"
    )
    for index in range(12):
        event_time = NOW + timedelta(minutes=index)
        await storage.insert_chain_events_uncommitted(
            [
                ChainEvent(
                    chain="ethereum" if index % 2 == 0 else "bsc",
                    block_number=1000 + index,
                    tx_hash=f"0x{index:064x}",
                    log_index=index,
                    event_type="Transfer",
                    payload={"value": index, "ignored": "value"},
                    observed_at=event_time,
                )
            ]
        )
        await storage.upsert_announcement_uncommitted(
            Announcement(
                source="wlfi",
                stable_id=f"notice-{index}",
                title=f"公告 {index}",
                url=(
                    f"https://docs.example/notices/{index}?token=hidden"
                    if index != 11
                    else "javascript:alert(1)"
                ),
                published_at=event_time,
                body_hash=f"body-{index}",
                first_seen_at=event_time,
                metadata={"ignored": "value"},
            )
        )
    await storage.connection.commit()
    repository = DashboardRepository(storage.path)
    await repository.open()
    try:
        recent = (await repository.snapshot(now=NOW))["recent"]
    finally:
        await repository.close()

    assert len(recent["alerts"]) == 10
    assert recent["alerts"][0]["content"] == "第一部分\n第二部分"
    assert all(item["content"] != "不应显示" for item in recent["alerts"])
    assert len(recent["chain_events"]) == 10
    assert recent["chain_events"][0]["block_number"] == 1011
    assert recent["chain_events"][0]["url"].startswith("https://bscscan.com/tx/")
    assert "payload" not in recent["chain_events"][0]
    assert len(recent["announcements"]) == 10
    assert recent["announcements"][0]["url"] is None
    assert recent["announcements"][1]["url"] == "https://docs.example/notices/10"
