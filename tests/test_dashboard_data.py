import asyncio
from datetime import UTC, datetime, timedelta
import json
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


async def dashboard_snapshot(storage, now: datetime = NOW) -> dict[str, object]:
    repository = DashboardRepository(storage.path)
    await repository.open()
    try:
        return await repository.snapshot(now=now)
    finally:
        await repository.close()


async def seed_custody_dashboard_observations(storage) -> None:
    address_rows = (
        (
            "ethereum:0x1111111111111111111111111111111111111111",
            2_000_000_000,
            {
                "entity": "binance_cex",
                "label": "Binance 28 <script>",
                "status": "trusted",
                "evidence_urls": [
                    "https://www.binance.com/en/square/post/97671?token=hidden"
                ],
                "verified_on": "2026-09-10",
                "max_age_seconds": 1200,
                "safe_block": 25_900_000,
            },
        ),
        (
            "bsc:0x2222222222222222222222222222222222222222",
            720_000_000,
            {
                "entity": "binance_peg_reserve",
                "label": "Binance-Peg Reserve",
                "status": "trusted",
                "evidence_urls": ["https://bscscan.com/address/0x2222"],
                "verified_on": "2026-09-10",
                "max_age_seconds": 1200,
                "safe_block": 120_900_000,
            },
        ),
        (
            "ethereum:0x3333333333333333333333333333333333333333",
            40_000_000,
            {
                "entity": "binance_cex",
                "label": "待核验 Binance 地址",
                "status": "candidate",
                "evidence_urls": ["javascript:alert(1)"],
                "verified_on": None,
                "max_age_seconds": 1200,
                "safe_block": 25_900_000,
            },
        ),
        (
            "solana:9Rycov3U4efJf5HiqZYGjN7qJJHEtMsj4vbmkG4xfCxk",
            5_000_000,
            {
                "entity": "fireblocks_custody",
                "label": "Fireblocks Custody",
                "status": "trusted",
                "evidence_urls": ["https://solscan.io/account/9Rycov3"],
                "verified_on": "2026-09-10",
                "max_age_seconds": 1200,
                "safe_block": 360_000_000,
            },
        ),
        (
            "ethereum:0x4444444444444444444444444444444444444444",
            10_000_000,
            {
                "entity": "bitgo_issuer",
                "label": "BitGo Issuer",
                "status": "trusted",
                "evidence_urls": ["https://www.bitgo.com/usd1/"],
                "verified_on": "2026-09-10",
                "max_age_seconds": 1200,
                "safe_block": 25_900_000,
            },
        ),
        (
            "bsc:0x5555555555555555555555555555555555555555",
            15_000_000,
            {
                "entity": "unlabeled_whale",
                "label": "未标注巨鲸",
                "status": "candidate",
                "evidence_urls": ["https://bscscan.com/address/0x5555"],
                "verified_on": None,
                "max_age_seconds": 1200,
                "safe_block": 120_900_000,
            },
        ),
    )
    for scope, value, metadata in address_rows:
        await insert_observation(
            storage,
            "custody.address_balance",
            scope,
            value,
            metadata=metadata,
        )
    await insert_observation(
        storage,
        "custody.binance_verified_balance",
        "global",
        2_720_000_000,
        metadata={"max_age_seconds": 1200},
    )
    await insert_observation(
        storage,
        "custody.binance_share_lower_bound",
        "global",
        0.68,
        unit="ratio",
        metadata={
            "verified_balance": 2_720_000_000,
            "supply": 4_000_000_000,
            "max_age_seconds": 1200,
        },
    )
    await insert_observation(
        storage,
        "custody.binance_net_change_24h",
        "global",
        -25_000_000,
        metadata={"status": "complete", "window_hours": 24},
    )
    await insert_observation(
        storage,
        "custody.address_external_outflow_1h",
        "ethereum:0x1111111111111111111111111111111111111111",
        12_000_000,
        metadata={"status": "complete", "window_hours": 1},
    )
    await insert_observation(
        storage,
        "custody.address_external_outflow_1h",
        "bsc:0x2222222222222222222222222222222222222222",
        0,
        metadata={"status": "complete", "window_hours": 1},
    )
    for hours, value in ((1, -1_000_000), (24, 2_000_000)):
        await insert_observation(
            storage,
            f"custody.solana_balance_delta_{hours}h",
            "solana:9Rycov3U4efJf5HiqZYGjN7qJJHEtMsj4vbmkG4xfCxk",
            value,
            metadata={
                "status": "complete",
                "window_hours": hours,
                "counterparty_attribution": False,
            },
        )


def unverified_media_lead(index: int = 0) -> Announcement:
    return Announcement(
        source="media_redemption",
        stable_id=f"lead-{index}",
        title=f"USD1 <b>bank</b> report {index}",
        url=f"https://news.example/reports/{index}?token=hidden",
        published_at=NOW - timedelta(minutes=index),
        body_hash=f"lead-body-{index}",
        first_seen_at=NOW,
        metadata={
            "verified": False,
            "lead_only": True,
            "summary": "Possible settlement issue <script>alert(1)</script>",
        },
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


@pytest.mark.asyncio
async def test_dashboard_exposes_verified_lower_bound_and_unverified_leads(
    storage,
) -> None:
    await seed_custody_dashboard_observations(storage)
    await insert_observation(
        storage,
        "redemption.channel_status",
        "global",
        0,
        unit="risk_level",
        metadata={
            "summary": "未发现官方限制",
            "source_url": "https://status.bitgo.com/?token=hidden",
            "max_age_seconds": 900,
        },
    )
    await storage.upsert_announcement(unverified_media_lead())

    snapshot = await dashboard_snapshot(storage)

    custody = snapshot["custody"]
    assert custody["available"] is True
    assert custody["binance_share_lower_bound"] == pytest.approx(0.68)
    assert custody["binance_verified_balance"] == 2_720_000_000
    assert custody["label"] == "已核验地址至少占比"
    assert custody["entities"]["binance_cex"]["balance"] == 2_000_000_000
    assert custody["entities"]["binance_peg_reserve"]["balance"] == 720_000_000
    assert custody["entities"]["candidate"]["balance"] == 55_000_000
    assert custody["entities"]["fireblocks_custody"]["balance"] == 5_000_000
    assert custody["entities"]["bitgo_issuer"]["balance"] == 10_000_000
    assert custody["entities"]["unlabeled_whale"]["balance"] == 15_000_000
    assert custody["binance_net_change_24h"]["value"] == -25_000_000
    assert custody["largest_address_outflow_1h"]["value"] == 12_000_000
    assert custody["largest_address_outflow_1h"]["label"] == "Binance 28 <script>"
    assert custody["largest_address_outflow_1h"]["chain"] == "ethereum"
    assert custody["solana"]["delta_1h"] == -1_000_000
    assert custody["solana"]["delta_24h"] == 2_000_000
    assert custody["solana"]["counterparty_attribution"] is False
    assert custody["addresses"][0].keys() >= {
        "chain",
        "address",
        "label",
        "status",
        "evidence_urls",
        "verified_on",
        "available",
    }
    assert custody["addresses"][0]["label"] == "Binance 28 <script>"
    assert custody["addresses"][0]["evidence_urls"] == [
        "https://www.binance.com/en/square/post/97671"
    ]
    candidate = next(
        item for item in custody["addresses"] if item["status"] == "candidate"
    )
    assert candidate["evidence_urls"] == []
    assert snapshot["redemption"]["available"] is True
    assert snapshot["redemption"]["summary"] == "未发现官方限制"
    assert snapshot["redemption"]["source_url"] == "https://status.bitgo.com/"
    leads = snapshot["recent"]["unverified_leads"]
    assert leads == [
        {
            "source": "media_redemption",
            "publisher": "news.example",
            "title": "USD1 <b>bank</b> report 0",
            "summary": "Possible settlement issue <script>alert(1)</script>",
            "url": "https://news.example/reports/0",
            "published_at": "2026-09-10T16:00:00+08:00",
            "first_seen_at": "2026-09-10T16:00:00+08:00",
            "verified": False,
        }
    ]
    assert all(
        item["source"] != "media_redemption"
        for item in snapshot["recent"]["announcements"]
    )


@pytest.mark.asyncio
async def test_dashboard_official_announcements_use_fixed_allowlist(storage) -> None:
    for source in (
        "binance",
        "bitgo",
        "wlfi",
        "occ",
        "redemption_page",
        "redemption_page_0",
        "redemption_page_12",
        "redemption_page_bad",
        "redemption_page_1bad",
        "unknown",
        "media_redemption",
    ):
        await storage.upsert_announcement(
            Announcement(
                source=source,
                stable_id=source,
                title=f"{source} notice",
                url=f"https://{source}.example/notice",
                published_at=NOW,
                body_hash=f"hash-{source}",
                first_seen_at=NOW,
            )
        )

    announcements = (await dashboard_snapshot(storage))["recent"]["announcements"]

    assert {item["source"] for item in announcements} == {
        "binance",
        "bitgo",
        "wlfi",
        "occ",
        "redemption_page",
        "redemption_page_0",
        "redemption_page_12",
    }


@pytest.mark.asyncio
async def test_dashboard_marks_stale_custody_and_redemption_as_last_known(
    storage,
) -> None:
    stale_at = NOW - timedelta(minutes=20)
    await insert_observation(
        storage,
        "custody.address_balance",
        "ethereum:0x1111111111111111111111111111111111111111",
        2_720_000_000,
        observed_at=stale_at,
        metadata={
            "entity": "binance_cex",
            "label": "Binance 28",
            "status": "trusted",
            "evidence_urls": [],
            "verified_on": "2026-09-10",
            "max_age_seconds": 1200,
            "safe_block": 25_900_000,
        },
    )
    await insert_observation(
        storage,
        "custody.binance_share_lower_bound",
        "global",
        0.68,
        observed_at=stale_at,
        unit="ratio",
        metadata={
            "verified_balance": 2_720_000_000,
            "max_age_seconds": 1200,
        },
    )
    await insert_observation(
        storage,
        "redemption.channel_status",
        "global",
        1,
        observed_at=NOW - timedelta(minutes=15),
        unit="risk_level",
        metadata={
            "summary": "结算延迟",
            "source_url": "https://status.bitgo.com/incidents/1",
            "max_age_seconds": 900,
        },
    )

    snapshot = await dashboard_snapshot(storage)

    assert snapshot["custody"]["available"] is False
    assert snapshot["custody"]["binance_share_lower_bound"] is None
    assert snapshot["custody"]["last_known"]["binance_share_lower_bound"] == 0.68
    assert snapshot["redemption"]["available"] is False
    assert snapshot["redemption"]["summary"] == "官方赎回通道数据暂不可用"
    assert snapshot["redemption"]["last_known"]["summary"] == "结算延迟"


@pytest.mark.asyncio
async def test_dashboard_marks_stale_por_as_last_known_without_coverage(
    storage,
) -> None:
    await seed_asset_pillars(storage, reserves_at=NOW - timedelta(minutes=30))
    await insert_observation(
        storage,
        "supply.estimated_collateralization",
        "global",
        1.02,
        unit="ratio",
    )

    snapshot = await dashboard_snapshot(storage)

    assert snapshot["metrics"]["reserves"]["available"] is False
    assert snapshot["metrics"]["reserves"]["last_known"] is True
    assert snapshot["metrics"]["reserves"]["value"] == 4_200_000_000
    assert snapshot["metrics"]["estimated_collateralization"] is None


@pytest.mark.asyncio
async def test_dashboard_returns_only_ten_unverified_media_leads(storage) -> None:
    for index in range(12):
        await storage.upsert_announcement(unverified_media_lead(index))
    await storage.upsert_announcement(
        Announcement(
            source="wlfi",
            stable_id="official",
            title="Official USD1 update",
            url="https://worldlibertyfinancial.com/usd1",
            published_at=NOW + timedelta(minutes=1),
            body_hash="official",
            first_seen_at=NOW,
        )
    )

    recent = (await dashboard_snapshot(storage))["recent"]

    assert len(recent["unverified_leads"]) == 10
    assert {item["source"] for item in recent["unverified_leads"]} == {
        "media_redemption"
    }
    assert [item["source"] for item in recent["announcements"]] == ["wlfi"]


@pytest.mark.asyncio
async def test_dashboard_does_not_publish_partial_solana_delta(storage) -> None:
    for scope, quality, status, value in (
        ("solana:trusted-ready", "FACT", "complete", 3_000_000),
        ("solana:trusted-waiting", "UNAVAILABLE", "accumulating", 0),
    ):
        await insert_observation(
            storage,
            "custody.solana_balance_delta_1h",
            scope,
            value,
            quality=quality,
            metadata={
                "status": status,
                "window_hours": 1,
                "counterparty_attribution": False,
            },
        )

    solana = (await dashboard_snapshot(storage))["custody"]["solana"]

    assert solana["delta_1h_available"] is False
    assert solana["delta_1h"] is None
    assert solana.get("delta_1h_last_known") is None


@pytest.mark.asyncio
async def test_dashboard_uses_completed_flow_as_last_known(storage) -> None:
    completed_at = NOW - timedelta(days=7)
    await insert_observation(
        storage,
        "custody.binance_net_change_24h",
        "global",
        -8_000_000,
        observed_at=completed_at,
        metadata={"status": "complete", "window_hours": 24},
    )
    await insert_observation(
        storage,
        "custody.binance_net_change_24h",
        "global",
        0,
        quality="UNAVAILABLE",
        metadata={"status": "accumulating", "window_hours": 24},
    )

    flow = (await dashboard_snapshot(storage))["custody"]["binance_net_change_24h"]

    assert flow["available"] is False
    assert flow["value"] is None
    assert flow["last_known"] == {
        "value": -8_000_000,
        "observed_at": completed_at.isoformat(),
    }


@pytest.mark.asyncio
async def test_dashboard_uses_coherent_completed_solana_cohort_as_last_known(
    storage,
) -> None:
    completed_at = NOW - timedelta(minutes=10)
    for scope, value in (("solana:a", 4_000_000), ("solana:b", -1_000_000)):
        await insert_observation(
            storage,
            "custody.solana_balance_delta_1h",
            scope,
            value,
            observed_at=completed_at,
            metadata={"status": "complete", "window_hours": 1},
        )
        await insert_observation(
            storage,
            "custody.solana_balance_delta_1h",
            scope,
            0,
            quality="UNAVAILABLE",
            metadata={"status": "accumulating", "window_hours": 1},
        )

    solana = (await dashboard_snapshot(storage))["custody"]["solana"]

    assert solana["delta_1h_available"] is False
    assert solana["delta_1h"] is None
    assert solana["delta_1h_last_known"] == 3_000_000


@pytest.mark.asyncio
async def test_dashboard_rejects_mixed_custody_snapshot(storage) -> None:
    await seed_custody_dashboard_observations(storage)
    partial_at = NOW + timedelta(minutes=5)
    await insert_observation(
        storage,
        "custody.address_balance",
        "ethereum:0x1111111111111111111111111111111111111111",
        2_100_000_000,
        observed_at=partial_at,
        metadata={
            "entity": "binance_cex",
            "label": "Binance 28",
            "status": "trusted",
            "evidence_urls": [],
            "verified_on": "2026-09-10",
            "max_age_seconds": 1200,
            "safe_block": 25_900_025,
        },
    )

    custody = (await dashboard_snapshot(storage, now=partial_at))["custody"]

    assert custody["available"] is False
    assert custody["binance_share_lower_bound"] is None
    assert custody["last_known"]["binance_share_lower_bound"] == pytest.approx(0.68)
    assert custody["entities"]["binance_cex"]["available"] is False
    assert custody["entities"]["binance_cex"]["last_known_balance"] == 2_000_000_000
    assert {item["observed_at"] for item in custody["addresses"]} == {
        "2026-09-10T16:00:00+08:00"
    }


@pytest.mark.asyncio
async def test_dashboard_rejects_mismatched_safe_block_in_custody_cohort(
    storage,
) -> None:
    await seed_custody_dashboard_observations(storage)
    await insert_observation(
        storage,
        "custody.address_balance",
        "ethereum:0x4444444444444444444444444444444444444444",
        10_000_000,
        metadata={
            "entity": "bitgo_issuer",
            "label": "BitGo Issuer",
            "status": "trusted",
            "evidence_urls": [],
            "verified_on": "2026-09-10",
            "max_age_seconds": 1200,
            "safe_block": 25_900_001,
        },
    )

    custody = (await dashboard_snapshot(storage))["custody"]

    assert custody["available"] is False
    assert custody["binance_share_lower_bound"] is None
    assert all(item["available"] is False for item in custody["addresses"])


@pytest.mark.asyncio
async def test_dashboard_rejects_partial_fact_solana_cohort(storage) -> None:
    completed_at = NOW - timedelta(minutes=10)
    for scope, value in (("solana:a", 4_000_000), ("solana:b", -1_000_000)):
        await insert_observation(
            storage,
            "custody.solana_balance_delta_1h",
            scope,
            value,
            observed_at=completed_at,
            metadata={"status": "complete", "window_hours": 1},
        )
    await insert_observation(
        storage,
        "custody.solana_balance_delta_1h",
        "solana:a",
        5_000_000,
        metadata={"status": "complete", "window_hours": 1},
    )

    solana = (await dashboard_snapshot(storage))["custody"]["solana"]

    assert solana["delta_1h_available"] is False
    assert solana["delta_1h"] is None
    assert solana["delta_1h_last_known"] == 3_000_000


@pytest.mark.asyncio
async def test_dashboard_outflow_uses_only_previous_complete_cohort_identity(
    storage,
) -> None:
    await seed_custody_dashboard_observations(storage)
    partial_at = NOW + timedelta(minutes=5)
    await insert_observation(
        storage,
        "custody.address_external_outflow_1h",
        "ethereum:0x1111111111111111111111111111111111111111",
        99_000_000,
        observed_at=partial_at,
        metadata={"status": "complete", "window_hours": 1},
    )

    outflow = (
        await dashboard_snapshot(storage, now=partial_at)
    )["custody"]["largest_address_outflow_1h"]

    assert outflow["available"] is False
    assert outflow["value"] is None
    assert outflow["last_known"]["value"] == 12_000_000
    assert outflow["scope"] == (
        "ethereum:0x1111111111111111111111111111111111111111"
    )
    assert outflow["label"] == "Binance 28 <script>"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_window", [True, 24.0, 1])
async def test_dashboard_rejects_invalid_24h_flow_window_metadata(
    storage, invalid_window: object
) -> None:
    completed_at = NOW - timedelta(minutes=10)
    await insert_observation(
        storage,
        "custody.binance_net_change_24h",
        "global",
        -8_000_000,
        observed_at=completed_at,
        metadata={"status": "complete", "window_hours": 24},
    )
    await insert_observation(
        storage,
        "custody.binance_net_change_24h",
        "global",
        77_000_000,
        metadata={"status": "complete", "window_hours": invalid_window},
    )

    flow = (await dashboard_snapshot(storage))["custody"][
        "binance_net_change_24h"
    ]

    assert flow["available"] is False
    assert flow["value"] is None
    assert flow["last_known"]["value"] == -8_000_000


@pytest.mark.asyncio
async def test_dashboard_rejects_wrong_solana_window_metadata(storage) -> None:
    await insert_observation(
        storage,
        "custody.solana_balance_delta_1h",
        "solana:a",
        7_000_000,
        metadata={"status": "complete", "window_hours": 24},
    )

    solana = (await dashboard_snapshot(storage))["custody"]["solana"]

    assert solana["delta_1h_available"] is False
    assert solana["delta_1h"] is None
    assert solana.get("delta_1h_last_known") is None


@pytest.mark.asyncio
async def test_dashboard_share_falls_back_only_to_previous_fact(storage) -> None:
    await seed_custody_dashboard_observations(storage)
    failed_at = NOW + timedelta(minutes=5)
    await insert_observation(
        storage,
        "custody.binance_share_lower_bound",
        "global",
        0,
        observed_at=failed_at,
        quality="UNAVAILABLE",
        unit="ratio",
        metadata={"max_age_seconds": 1200},
    )

    custody = (await dashboard_snapshot(storage, now=failed_at))["custody"]

    assert custody["available"] is False
    assert custody["binance_share_lower_bound"] is None
    assert custody["last_known"]["binance_share_lower_bound"] == pytest.approx(0.68)
    assert all(item["available"] is False for item in custody["addresses"])


@pytest.mark.asyncio
async def test_dashboard_share_has_no_last_known_without_fact(storage) -> None:
    await insert_observation(
        storage,
        "custody.binance_share_lower_bound",
        "global",
        0,
        quality="UNAVAILABLE",
        unit="ratio",
        metadata={"max_age_seconds": 1200},
    )

    custody = (await dashboard_snapshot(storage))["custody"]

    assert custody["available"] is False
    assert custody["last_known"] is None


@pytest.mark.asyncio
async def test_dashboard_redemption_falls_back_only_to_previous_fact(storage) -> None:
    completed_at = NOW - timedelta(minutes=10)
    await insert_observation(
        storage,
        "redemption.channel_status",
        "global",
        1,
        observed_at=completed_at,
        unit="risk_level",
        metadata={
            "summary": "结算延迟",
            "source_url": "https://status.bitgo.com/incidents/1",
            "max_age_seconds": 900,
        },
    )
    await insert_observation(
        storage,
        "redemption.channel_status",
        "global",
        0,
        quality="UNAVAILABLE",
        unit="risk_level",
        metadata={"summary": "错误的正常状态", "max_age_seconds": 900},
    )

    redemption = (await dashboard_snapshot(storage))["redemption"]

    assert redemption["available"] is False
    assert redemption["level"] == "UNKNOWN"
    assert redemption["summary"] == "官方赎回通道数据暂不可用"
    assert redemption["last_known"]["level"] == "YELLOW"
    assert redemption["last_known"]["summary"] == "结算延迟"


@pytest.mark.asyncio
async def test_complete_cohort_search_is_single_query_and_bounded(storage) -> None:
    metric = "custody.solana_balance_delta_1h"
    rows = []
    for index in range(520):
        observed_at = NOW - timedelta(minutes=index + 1)
        rows.append(
            (
                metric,
                "custody",
                "solana:a",
                float(index),
                "USD1",
                observed_at.isoformat(),
                observed_at.isoformat(),
                "FACT",
                json.dumps({"status": "complete", "window_hours": 1}),
            )
        )
    await storage.connection.executemany(
        """
        INSERT INTO observations(
            metric,source,scope,value,unit,observed_at,collected_at,
            quality,metadata_json
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        rows,
    )
    await storage.connection.commit()

    repository = DashboardRepository(storage.path)
    await repository.open()
    statements: list[str] = []
    await repository.connection.set_trace_callback(statements.append)
    try:
        cohort = await repository._latest_complete_cohort(
            metric,
            before=NOW,
            expected_scopes={"solana:a", "solana:b"},
            expected_window_hours=1,
        )
    finally:
        await repository.close()

    selects = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT")
    ]
    assert cohort == []
    assert len(selects) == 1
    assert "LIMIT 513" in selects[0]
