from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from usd1_monitor.collectors.redemption import RedemptionStatusSnapshot
from usd1_monitor.config import RedemptionConfig
from usd1_monitor.models import Announcement, Observation, RiskLevel
from usd1_monitor.scheduler import RedemptionChannelMonitor


NOW = datetime(2026, 9, 11, 4, 0, tzinfo=UTC)


def _announcement(
    source: str,
    stable_id: str,
    sections: list[str],
    *,
    body_hash: str = "v1",
    first_seen_at: datetime = NOW,
) -> Announcement:
    url = (
        f"https://www.bitgo.com{stable_id}".rstrip("/")
        if source.startswith("redemption_page")
        else f"https://official.example/{stable_id}"
    )
    return Announcement(
        source=source,
        stable_id=stable_id,
        title=sections[0],
        url=url,
        published_at=None,
        body_hash=body_hash,
        first_seen_at=first_seen_at,
        metadata={"body_sections": sections, "body_text": " ".join(sections)},
    )


class FakeStatusCollector:
    def __init__(self, snapshots: list[RedemptionStatusSnapshot]) -> None:
        self.snapshots = snapshots
        self.calls = 0

    async def collect(self, checked_at: datetime) -> RedemptionStatusSnapshot:
        self.calls += 1
        snapshot = self.snapshots.pop(0)
        observation = Observation(
            metric="redemption.channel_status",
            source="bitgo_status",
            scope="global",
            value=float(snapshot.level),
            unit="risk_level",
            observed_at=checked_at,
            collected_at=checked_at,
            metadata={**snapshot.observation.metadata},
        )
        return RedemptionStatusSnapshot(
            snapshot.level,
            snapshot.summary,
            snapshot.confirmed_usd1,
            snapshot.matched_text,
            observation,
        )


class FakePageCollector:
    def __init__(self, items: list[Announcement]) -> None:
        self.items = items
        self.source = items[0].source
        self.url = items[0].url
        self.calls = 0

    async def collect(self, checked_at: datetime) -> Announcement:
        self.calls += 1
        item = self.items.pop(0)
        return Announcement(
            item.source,
            item.stable_id,
            item.title,
            item.url,
            item.published_at,
            item.body_hash,
            checked_at,
            item.metadata,
        )


class FailingPageCollector:
    source = "redemption_page_bitgo"
    url = "https://www.bitgo.com/usd1/"

    async def collect(self, checked_at: datetime) -> Announcement:
        raise RuntimeError("page unavailable")


class ConcurrentStatusCollector:
    def __init__(self, started: set[str], ready: asyncio.Event) -> None:
        self.started = started
        self.ready = ready

    async def collect(self, checked_at: datetime) -> RedemptionStatusSnapshot:
        self.started.add("status")
        if len(self.started) == 3:
            self.ready.set()
        await self.ready.wait()
        return _status()


class ConcurrentPageCollector:
    source = "redemption_page_bitgo"
    url = "https://www.bitgo.com/usd1/"

    def __init__(self, started: set[str], ready: asyncio.Event) -> None:
        self.started = started
        self.ready = ready

    async def collect(self, checked_at: datetime) -> Announcement:
        self.started.add("page")
        if len(self.started) == 3:
            self.ready.set()
        await self.ready.wait()
        return _announcement(self.source, "/usd1/", ["USD1 terms"])


class ConcurrentMediaCollector:
    def __init__(self, started: set[str], ready: asyncio.Event) -> None:
        self.started = started
        self.ready = ready

    async def collect(self, checked_at: datetime) -> list[Announcement]:
        self.started.add("media")
        if len(self.started) == 3:
            self.ready.set()
        await self.ready.wait()
        return []


class FakeMediaCollector:
    def __init__(self, items: list[Announcement]) -> None:
        self.items = items

    async def collect(self, checked_at: datetime) -> list[Announcement]:
        return self.items


def _status(
    level: RiskLevel = RiskLevel.GREEN,
    *,
    summary: str = "未发现官方限制",
    matched_text: str | None = None,
    confirmed_usd1: bool = False,
) -> RedemptionStatusSnapshot:
    return RedemptionStatusSnapshot(
        level,
        summary,
        confirmed_usd1,
        matched_text,
        Observation(
            "redemption.channel_status",
            "bitgo_status",
            "global",
            float(level),
            "risk_level",
            NOW,
            NOW,
            metadata={"source_url": "https://status.bitgo.com", "max_age_seconds": 900},
        ),
    )


def _monitor(
    storage,
    *,
    statuses: list[RedemptionStatusSnapshot] | None = None,
    pages: list[FakePageCollector] | None = None,
    media: list[FakeMediaCollector] | None = None,
) -> RedemptionChannelMonitor:
    effective_pages = pages
    if effective_pages is None:
        effective_pages = [
            FakePageCollector(
                [_announcement("redemption_page_bitgo", "/usd1/", ["USD1 terms"])]
            )
        ]
    return RedemptionChannelMonitor(
        FakeStatusCollector(statuses or [_status()]),
        effective_pages,
        media or [],
        storage,
        RedemptionConfig(
            status_interval_seconds=300,
            page_interval_seconds=3600,
            recovery_checks=2,
            official_page_urls=[collector.url for collector in effective_pages],
        ),
    )


@pytest.mark.asyncio
async def test_redemption_baseline_is_clear_without_alert(storage) -> None:
    page = FakePageCollector(
        [
            _announcement(
                "redemption_page_bitgo",
                "/usd1/",
                ["USD1 terms", "BitGo may suspend redemptions under these terms"],
            )
        ]
    )
    monitor = _monitor(storage, pages=[page])

    result = await monitor.check_once(deliver=False, now=NOW)

    assert result.success
    state = await storage.get_risk_state("redemption.channel")
    assert state is not None and state.level is RiskLevel.GREEN
    assert await storage.count_alert_deliveries() == 0
    source = await storage.latest_observation(
        "redemption.source_status", "page:https://www.bitgo.com/usd1"
    )
    assert source is not None and source.value == RiskLevel.GREEN


@pytest.mark.asyncio
async def test_changed_page_explicit_suspension_is_red_and_alertable(storage) -> None:
    page = FakePageCollector(
        [
            _announcement("redemption_page_bitgo", "/usd1/", ["USD1 terms"]),
            _announcement(
                "redemption_page_bitgo",
                "/usd1/",
                ["USD1 terms", "USD1 redemptions are suspended"],
                body_hash="v2",
            ),
        ]
    )
    monitor = _monitor(storage, statuses=[_status(), _status()], pages=[page])
    await monitor.check_once(deliver=False, now=NOW)

    result = await monitor.check_once(
        deliver=False, now=NOW + timedelta(hours=1)
    )

    assert result.success
    state = await storage.get_risk_state("redemption.channel")
    assert state is not None and state.level is RiskLevel.RED
    assert await storage.count_alert_deliveries() == 1
    aggregate = await storage.latest_observation(
        "redemption.channel_status", "global"
    )
    assert aggregate is not None
    assert aggregate.metadata["summary"] == "USD1 官方赎回已暂停或不可用"
    assert aggregate.metadata["confirmed_usd1"] is True
    assert aggregate.metadata["matched_text"] == "USD1 redemptions are suspended"


@pytest.mark.asyncio
async def test_media_lead_is_stored_without_risk_or_alert(storage) -> None:
    lead = _announcement(
        "media_redemption", "lead", ["USD1 redemption report"]
    )
    monitor = _monitor(storage, media=[FakeMediaCollector([lead])])

    result = await monitor.check_once(deliver=False, now=NOW)

    assert result.success
    assert await storage.latest_announcement("media_redemption") is not None
    assert await storage.get_risk_state("event.information.media_redemption") is None
    assert await storage.count_alert_deliveries() == 0


@pytest.mark.asyncio
async def test_operational_status_does_not_clear_active_page_suspension(storage) -> None:
    page = FakePageCollector(
        [
            _announcement("redemption_page_bitgo", "/usd1/", ["USD1 terms"]),
            _announcement(
                "redemption_page_bitgo",
                "/usd1/",
                ["USD1 terms", "USD1 redemptions are suspended"],
                body_hash="v2",
            ),
        ]
    )
    monitor = _monitor(
        storage,
        statuses=[_status(), _status(), _status()],
        pages=[page],
    )
    await monitor.check_once(deliver=False, now=NOW)
    await monitor.check_once(deliver=False, now=NOW + timedelta(hours=1))

    await monitor.check_once(
        deliver=False, now=NOW + timedelta(hours=1, minutes=5)
    )

    state = await storage.get_risk_state("redemption.channel")
    assert state is not None and state.level is RiskLevel.RED
    assert await storage.count_alert_deliveries() == 1


@pytest.mark.asyncio
async def test_explicit_recovery_requires_two_green_aggregations(storage) -> None:
    page = FakePageCollector(
        [
            _announcement("redemption_page_bitgo", "/usd1/", ["USD1 terms"]),
            _announcement(
                "redemption_page_bitgo",
                "/usd1/",
                ["USD1 terms", "USD1 redemptions are suspended"],
                body_hash="v2",
            ),
            _announcement(
                "redemption_page_bitgo",
                "/usd1/",
                [
                    "USD1 terms",
                    "USD1 redemptions are suspended",
                    "USD1 redemptions are now operational",
                ],
                body_hash="v3",
            ),
        ]
    )
    monitor = _monitor(
        storage,
        statuses=[_status(), _status(), _status(), _status()],
        pages=[page],
    )
    await monitor.check_once(deliver=False, now=NOW)
    await monitor.check_once(deliver=False, now=NOW + timedelta(hours=1))

    await monitor.check_once(deliver=False, now=NOW + timedelta(hours=2))
    first = await storage.get_risk_state("redemption.channel")
    assert first is not None and first.level is RiskLevel.RED
    held = await storage.latest_observation("redemption.channel_status", "global")
    assert held is not None and held.metadata["clear_checks"] == 1
    assert held.metadata["data_time"] == (NOW + timedelta(hours=1)).isoformat()

    await monitor.check_once(
        deliver=False, now=NOW + timedelta(hours=2, minutes=5)
    )
    recovered = await storage.get_risk_state("redemption.channel")
    assert recovered is not None and recovered.level is RiskLevel.GREEN
    assert await storage.count_alert_deliveries() == 2


@pytest.mark.asyncio
async def test_stale_required_page_does_not_refresh_channel_status(storage) -> None:
    page = FakePageCollector(
        [_announcement("redemption_page_bitgo", "/usd1/", ["USD1 terms"])]
    )
    monitor = _monitor(storage, statuses=[_status(), _status()], pages=[page])
    await monitor.check_once(deliver=False, now=NOW)
    before = await storage.latest_observation("redemption.channel_status", "global")
    assert before is not None

    result = await monitor.check_once(
        deliver=False, now=NOW + timedelta(hours=2, seconds=1)
    )

    assert not result.success
    after = await storage.latest_observation("redemption.channel_status", "global")
    assert after is not None and after.observed_at == before.observed_at


@pytest.mark.asyncio
async def test_restart_uses_fresh_page_status_when_page_retry_fails(storage) -> None:
    first = _monitor(
        storage,
        pages=[
            FakePageCollector(
                [_announcement("redemption_page_bitgo", "/usd1/", ["USD1 terms"])]
            )
        ],
    )
    await first.check_once(deliver=False, now=NOW)
    before = await storage.latest_observation("redemption.channel_status", "global")
    assert before is not None
    restarted = _monitor(
        storage,
        statuses=[_status()],
        pages=[FailingPageCollector()],
    )

    result = await restarted.check_once(
        deliver=False, now=NOW + timedelta(minutes=30)
    )

    assert not result.success
    after = await storage.latest_observation("redemption.channel_status", "global")
    assert after is not None and after.observed_at == NOW + timedelta(minutes=30)


@pytest.mark.asyncio
async def test_new_official_announcement_participates_but_unrelated_one_cannot_clear(
    storage,
) -> None:
    page = FakePageCollector(
        [_announcement("redemption_page_bitgo", "/usd1/", ["USD1 terms"])]
    )
    monitor = _monitor(storage, statuses=[_status(), _status()], pages=[page])
    await monitor.check_once(deliver=False, now=NOW)
    await storage.upsert_announcement(
        _announcement(
            "binance",
            "risk",
            ["USD1 redemptions are suspended"],
            first_seen_at=NOW + timedelta(minutes=1),
        )
    )

    await monitor.check_once(deliver=False, now=NOW + timedelta(minutes=5))
    state = await storage.get_risk_state("redemption.channel")
    assert state is not None and state.level is RiskLevel.RED

    await storage.upsert_announcement(
        _announcement(
            "binance",
            "general",
            ["Binance systems are operational"],
            first_seen_at=NOW + timedelta(minutes=6),
        )
    )
    await monitor.check_once(deliver=False, now=NOW + timedelta(minutes=10))
    state = await storage.get_risk_state("redemption.channel")
    assert state is not None and state.level is RiskLevel.RED


@pytest.mark.asyncio
async def test_new_official_announcement_title_participates(storage) -> None:
    page = FakePageCollector(
        [_announcement("redemption_page_bitgo", "/usd1/", ["USD1 terms"])]
    )
    monitor = _monitor(storage, statuses=[_status(), _status()], pages=[page])
    await monitor.check_once(deliver=False, now=NOW)
    item = _announcement(
        "wlfi",
        "risk-title",
        ["General details"],
        first_seen_at=NOW + timedelta(minutes=1),
    )
    await storage.upsert_announcement(
        Announcement(
            item.source,
            item.stable_id,
            "USD1 redemptions are suspended",
            item.url,
            item.published_at,
            item.body_hash,
            item.first_seen_at,
            item.metadata,
        )
    )

    await monitor.check_once(deliver=False, now=NOW + timedelta(minutes=5))

    state = await storage.get_risk_state("redemption.channel")
    assert state is not None and state.level is RiskLevel.RED


@pytest.mark.asyncio
async def test_unprocessed_official_risk_is_not_ignored_because_it_is_old(
    storage,
) -> None:
    await storage.upsert_announcement(
        _announcement(
            "occ",
            "older-risk",
            ["USD1 redemptions are suspended"],
            first_seen_at=NOW - timedelta(days=2),
        )
    )
    monitor = _monitor(storage)

    await monitor.check_once(deliver=False, now=NOW)

    state = await storage.get_risk_state("redemption.channel")
    assert state is not None and state.level is RiskLevel.RED


@pytest.mark.asyncio
async def test_official_announcements_are_applied_in_publication_order(storage) -> None:
    recovery = _announcement("wlfi", "recovery", ["USD1 redemptions are now operational"])
    risk = _announcement("wlfi", "risk-first", ["USD1 redemptions are suspended"])
    await storage.upsert_announcement(
        Announcement(
            recovery.source,
            recovery.stable_id,
            recovery.title,
            recovery.url,
            NOW - timedelta(hours=1),
            recovery.body_hash,
            recovery.first_seen_at,
            recovery.metadata,
        )
    )
    await storage.upsert_announcement(
        Announcement(
            risk.source,
            risk.stable_id,
            risk.title,
            risk.url,
            NOW - timedelta(hours=2),
            risk.body_hash,
            risk.first_seen_at,
            risk.metadata,
        )
    )
    monitor = _monitor(storage)

    await monitor.check_once(deliver=False, now=NOW)

    state = await storage.get_risk_state("redemption.channel")
    assert state is not None and state.level is RiskLevel.GREEN
    assert await storage.count_alert_deliveries() == 0


@pytest.mark.asyncio
async def test_older_recovery_announcement_cannot_clear_newer_page_risk(storage) -> None:
    page = FakePageCollector(
        [
            _announcement("redemption_page_bitgo", "/usd1/", ["USD1 terms"]),
            _announcement(
                "redemption_page_bitgo",
                "/usd1/",
                ["USD1 terms", "USD1 redemptions are suspended"],
                body_hash="v2",
            ),
        ]
    )
    monitor = _monitor(
        storage,
        statuses=[_status(), _status(), _status(), _status()],
        pages=[page],
    )
    await monitor.check_once(deliver=False, now=NOW)
    await monitor.check_once(deliver=False, now=NOW + timedelta(hours=1))
    old_recovery = _announcement(
        "binance", "old-recovery", ["USD1 redemptions are now operational"]
    )
    await storage.upsert_announcement(
        Announcement(
            old_recovery.source,
            old_recovery.stable_id,
            old_recovery.title,
            old_recovery.url,
            NOW - timedelta(days=1),
            old_recovery.body_hash,
            NOW + timedelta(hours=1, minutes=1),
            old_recovery.metadata,
        )
    )

    await monitor.check_once(
        deliver=False, now=NOW + timedelta(hours=1, minutes=5)
    )
    await monitor.check_once(
        deliver=False, now=NOW + timedelta(hours=1, minutes=10)
    )

    state = await storage.get_risk_state("redemption.channel")
    assert state is not None and state.level is RiskLevel.RED


@pytest.mark.asyncio
async def test_new_recovery_text_on_old_announcement_can_clear_current_risk(
    storage,
) -> None:
    old_news = _announcement("binance", "updated-news", ["USD1 information"])
    await storage.upsert_announcement(
        Announcement(
            old_news.source,
            old_news.stable_id,
            old_news.title,
            old_news.url,
            NOW - timedelta(days=1),
            old_news.body_hash,
            NOW - timedelta(days=1),
            old_news.metadata,
        )
    )
    page = FakePageCollector(
        [
            _announcement("redemption_page_bitgo", "/usd1/", ["USD1 terms"]),
            _announcement(
                "redemption_page_bitgo",
                "/usd1/",
                ["USD1 terms"],
            ),
        ]
    )
    monitor = _monitor(
        storage,
        statuses=[_status(), _status(), _status(), _status()],
        pages=[page],
    )
    await monitor.check_once(deliver=False, now=NOW)
    await storage.upsert_announcement(
        _announcement(
            "binance",
            "risk-current",
            ["USD1 redemptions are suspended"],
            first_seen_at=NOW + timedelta(hours=1),
        )
    )
    await monitor.check_once(deliver=False, now=NOW + timedelta(hours=1))
    await storage.upsert_announcement(
        Announcement(
            old_news.source,
            old_news.stable_id,
            "USD1 redemptions are now operational",
            old_news.url,
            NOW - timedelta(days=1),
            "v2",
            old_news.first_seen_at,
            {
                "body_sections": ["USD1 redemptions are now operational"],
                "body_text": "USD1 redemptions are now operational",
            },
        )
    )

    await monitor.check_once(
        deliver=False, now=NOW + timedelta(hours=1, minutes=5)
    )
    await monitor.check_once(
        deliver=False, now=NOW + timedelta(hours=1, minutes=10)
    )

    state = await storage.get_risk_state("redemption.channel")
    assert state is not None and state.level is RiskLevel.GREEN


@pytest.mark.asyncio
async def test_official_recovery_and_cause_clear_are_atomic(
    storage, monkeypatch
) -> None:
    page = FakePageCollector(
        [
            _announcement("redemption_page_bitgo", "/usd1/", ["USD1 terms"]),
            _announcement(
                "redemption_page_bitgo",
                "/usd1/",
                ["USD1 terms"],
            ),
        ]
    )
    monitor = _monitor(
        storage,
        statuses=[_status(), _status(), _status()],
        pages=[page],
    )
    await monitor.check_once(deliver=False, now=NOW)
    await storage.upsert_announcement(
        _announcement(
            "wlfi",
            "risk-atomic",
            ["USD1 redemptions are suspended"],
            first_seen_at=NOW + timedelta(hours=1),
        )
    )
    await monitor.check_once(deliver=False, now=NOW + timedelta(hours=1))
    await storage.upsert_announcement(
        _announcement(
            "wlfi",
            "recovery-atomic",
            ["USD1 redemptions are now operational"],
            first_seen_at=NOW + timedelta(hours=1, minutes=1),
        )
    )
    original = storage.insert_observation_uncommitted

    async def fail_cause_clear(observation: Observation) -> None:
        if (
            observation.metric == "redemption.source_status"
            and observation.scope == "announcement:wlfi:risk-atomic"
            and observation.value == 0
        ):
            raise RuntimeError("simulated cause clear failure")
        await original(observation)

    monkeypatch.setattr(storage, "insert_observation_uncommitted", fail_cause_clear)

    with pytest.raises(RuntimeError, match="simulated cause clear failure"):
        await monitor.check_once(
            deliver=False, now=NOW + timedelta(hours=1, minutes=5)
        )

    recovery_status = await storage.latest_observation(
        "redemption.source_status", "announcement:wlfi:recovery-atomic"
    )
    assert recovery_status is None


@pytest.mark.asyncio
async def test_failed_required_source_does_not_advance_clear_or_extend_freshness(
    storage,
) -> None:
    page = FakePageCollector(
        [
            _announcement("redemption_page_bitgo", "/usd1/", ["USD1 terms"]),
            _announcement(
                "redemption_page_bitgo",
                "/usd1/",
                ["USD1 terms", "USD1 redemptions are suspended"],
                body_hash="v2",
            ),
            _announcement(
                "redemption_page_bitgo",
                "/usd1/",
                [
                    "USD1 terms",
                    "USD1 redemptions are suspended",
                    "USD1 redemptions are now operational",
                ],
                body_hash="v3",
            ),
            _announcement(
                "redemption_page_bitgo",
                "/usd1/",
                [
                    "USD1 terms",
                    "USD1 redemptions are suspended",
                    "USD1 redemptions are now operational",
                ],
                body_hash="v3",
            ),
        ]
    )
    monitor = RedemptionChannelMonitor(
        FakeStatusCollector([_status()]),
        [page],
        [],
        storage,
        RedemptionConfig(
            status_interval_seconds=300,
            page_interval_seconds=300,
            recovery_checks=2,
            official_page_urls=["https://www.bitgo.com/usd1/"],
        ),
    )
    await monitor.check_once(deliver=False, now=NOW)
    await monitor.check_once(deliver=False, now=NOW + timedelta(minutes=5))

    await monitor.check_once(deliver=False, now=NOW + timedelta(minutes=10))

    aggregate = await storage.latest_observation(
        "redemption.channel_status", "global"
    )
    assert aggregate is not None
    assert aggregate.value == RiskLevel.RED
    assert aggregate.metadata["clear_checks"] == 0
    assert aggregate.metadata["max_age_seconds"] == 300

    await monitor.check_once(deliver=False, now=NOW + timedelta(minutes=15))
    expired = await storage.latest_observation(
        "redemption.channel_status", "global"
    )
    assert expired is not None
    assert expired.observed_at == aggregate.observed_at


@pytest.mark.asyncio
async def test_due_redemption_sources_collect_concurrently(storage) -> None:
    started: set[str] = set()
    ready = asyncio.Event()
    monitor = RedemptionChannelMonitor(
        ConcurrentStatusCollector(started, ready),
        [ConcurrentPageCollector(started, ready)],
        [ConcurrentMediaCollector(started, ready)],
        storage,
        RedemptionConfig(official_page_urls=["https://www.bitgo.com/usd1/"]),
    )

    result = await asyncio.wait_for(
        monitor.check_once(deliver=False, now=NOW), timeout=1
    )

    assert result.success
    assert started == {"status", "page", "media"}


@pytest.mark.asyncio
async def test_other_authority_recovery_cannot_clear_page_risk(storage) -> None:
    page = FakePageCollector(
        [
            _announcement("redemption_page_bitgo", "/usd1/", ["USD1 terms"]),
            _announcement(
                "redemption_page_bitgo",
                "/usd1/",
                ["USD1 terms", "USD1 redemptions are suspended"],
                body_hash="v2",
            ),
        ]
    )
    monitor = _monitor(
        storage,
        statuses=[_status(), _status(), _status(), _status()],
        pages=[page],
    )
    await monitor.check_once(deliver=False, now=NOW)
    await monitor.check_once(deliver=False, now=NOW + timedelta(hours=1))
    await storage.upsert_announcement(
        _announcement(
            "binance",
            "unrelated-recovery",
            ["USD1 redemptions are now operational"],
            first_seen_at=NOW + timedelta(hours=1, minutes=1),
        )
    )

    await monitor.check_once(
        deliver=False, now=NOW + timedelta(hours=1, minutes=5)
    )
    await monitor.check_once(
        deliver=False, now=NOW + timedelta(hours=1, minutes=10)
    )

    state = await storage.get_risk_state("redemption.channel")
    assert state is not None and state.level is RiskLevel.RED


@pytest.mark.asyncio
async def test_replaced_page_url_does_not_reuse_old_page_status(storage) -> None:
    first = _monitor(
        storage,
        pages=[
            FakePageCollector(
                [_announcement("redemption_page_bitgo", "/usd1/", ["USD1 terms"])]
            )
        ],
    )
    await first.check_once(deliver=False, now=NOW)
    before = await storage.latest_observation("redemption.channel_status", "global")
    restarted = RedemptionChannelMonitor(
        FakeStatusCollector([_status()]),
        [FailingPageCollector()],
        [],
        storage,
        RedemptionConfig(
            official_page_urls=["https://www.bitgo.com/usd1-terms/"]
        ),
    )

    result = await restarted.check_once(
        deliver=False, now=NOW + timedelta(minutes=30)
    )

    assert not result.success
    after = await storage.latest_observation("redemption.channel_status", "global")
    assert before is not None and after is not None
    assert after.observed_at == before.observed_at


@pytest.mark.asyncio
async def test_reordered_page_urls_keep_status_bound_to_the_url(storage) -> None:
    first = _monitor(
        storage,
        statuses=[_status(), _status()],
        pages=[
            FakePageCollector(
                [
                    _announcement("redemption_page_0", "/usd1/", ["USD1 terms"]),
                    _announcement(
                        "redemption_page_0",
                        "/usd1/",
                        ["USD1 terms", "USD1 redemptions are suspended"],
                        body_hash="usd1-v2",
                    ),
                ]
            ),
            FakePageCollector(
                [
                    _announcement("redemption_page_1", "/terms/", ["Terms"]),
                    _announcement("redemption_page_1", "/terms/", ["Terms"]),
                ]
            ),
        ],
    )
    await first.check_once(deliver=False, now=NOW)
    await first.check_once(deliver=False, now=NOW + timedelta(hours=1))
    restarted = _monitor(
        storage,
        pages=[
            FakePageCollector(
                [_announcement("redemption_page_0", "/terms/", ["Terms"])]
            ),
            FakePageCollector(
                [
                    _announcement(
                        "redemption_page_1",
                        "/usd1/",
                        ["USD1 terms", "USD1 redemptions are suspended"],
                        body_hash="usd1-v2",
                    )
                ]
            ),
        ],
    )

    await restarted.check_once(
        deliver=False, now=NOW + timedelta(hours=1, minutes=5)
    )

    usd1_status = await storage.latest_observation(
        "redemption.source_status", "page:https://www.bitgo.com/usd1"
    )
    aggregate = await storage.latest_observation(
        "redemption.channel_status", "global"
    )
    assert usd1_status is not None and usd1_status.value == RiskLevel.RED
    assert aggregate is not None and aggregate.metadata["clear_checks"] == 0


@pytest.mark.asyncio
async def test_rolled_back_announcement_never_creates_redemption_status(
    storage, monkeypatch
) -> None:
    monitor = _monitor(storage)
    await monitor.check_once(deliver=False, now=NOW)
    original = storage.announcements_for_sources
    snapshot_read = asyncio.Event()

    async def observe_snapshot(sources: tuple[str, ...]) -> list[Announcement]:
        items = await original(sources)
        snapshot_read.set()
        return items

    monkeypatch.setattr(storage, "announcements_for_sources", observe_snapshot)
    connection = storage.connection
    task: asyncio.Task | None = None
    async with storage.write_lock:
        await connection.execute("BEGIN IMMEDIATE")
        try:
            await storage.upsert_announcement_uncommitted(
                _announcement(
                    "binance",
                    "rolled-back",
                    ["USD1 redemptions are suspended"],
                    first_seen_at=NOW + timedelta(minutes=1),
                )
            )
            task = asyncio.create_task(
                monitor.check_once(
                    deliver=False, now=NOW + timedelta(minutes=1)
                )
            )
            try:
                await asyncio.wait_for(snapshot_read.wait(), timeout=0.1)
            except TimeoutError:
                pass
        finally:
            await connection.rollback()
    assert task is not None

    result = await task

    source_status = await storage.latest_observation(
        "redemption.source_status", "announcement:binance:rolled-back"
    )
    state = await storage.get_risk_state("redemption.channel")
    assert result.success
    assert source_status is None
    assert state is not None and state.level is RiskLevel.GREEN
