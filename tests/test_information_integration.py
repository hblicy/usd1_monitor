from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes import FakeNotifier
import usd1_monitor.scheduler as scheduler_module
from usd1_monitor.collectors.announcements import (
    Announcement,
    BinanceAnnouncementCollector,
    BinancePartialCollectionError,
    content_hash,
)
from usd1_monitor.scheduler import InformationMonitor
from usd1_monitor.engine.state import StateEngine
from usd1_monitor.engine.aggregate import business_overall


NOW = datetime(2026, 9, 7, 4, 0, tzinfo=UTC)


class FakeOfficialSource:
    def __init__(self, values) -> None:
        self.values = list(values)

    async def collect(self, collected_at):
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def occ_item() -> Announcement:
    title = "OCC charter decision for World Liberty Financial USD1"
    return Announcement(
        "occ",
        "1385",
        title,
        "https://www.occ.gov/2026/cd1385.pdf",
        None,
        content_hash(title, "conditional approval"),
        NOW,
    )


def bitgo_item(month: str) -> Announcement:
    title = f"USD1 {month} attestation"
    return Announcement(
        "bitgo",
        f"{month}:hash",
        title,
        f"https://landing.bitgo.com/USD1_{month}.pdf",
        None,
        content_hash(title, month),
        NOW,
    )


def detailed_item(source: str, stable_id: str, title: str, body: str) -> Announcement:
    return Announcement(
        source,
        stable_id,
        title,
        f"https://www.{source}.com/{stable_id}",
        None,
        content_hash(title, body),
        NOW,
        {"body_text": body},
    )


@pytest.mark.asyncio
async def test_broken_binance_does_not_block_occ_and_gets_health_risk(
    storage,
) -> None:
    monitor = InformationMonitor(
        {
            "binance": FakeOfficialSource(
                [RuntimeError("layout changed")] * 3
            ),
            "occ": FakeOfficialSource([[occ_item()]]),
        },
        storage,
        FakeNotifier(),
        intervals={"binance": 900, "occ": 21600},
    )

    await monitor.check_once(now=NOW)
    await monitor.check_once(now=NOW + timedelta(minutes=15))
    result = await monitor.check_once(now=NOW + timedelta(minutes=30))

    assert await storage.latest_announcement("occ") is not None
    health = await storage.get_risk_state("health.official_binance")
    assert health is not None
    assert health.level.name == "YELLOW"
    assert result.success is False
    assert "binance" not in monitor._last_runs


@pytest.mark.asyncio
async def test_binance_partial_failure_persists_risk_and_records_failure(
    storage,
) -> None:
    risk_item = detailed_item(
        "binance",
        "later-risk",
        "General service update",
        "USD1 withdrawals are restricted",
    )
    partial_error = BinancePartialCollectionError(
        [risk_item],
        [("bad-detail", RuntimeError("detail request failed"))],
    )
    monitor = InformationMonitor(
        {"binance": FakeOfficialSource([partial_error])},
        storage,
        None,
        intervals={"binance": 900},
    )

    result = await monitor.check_once(now=NOW)

    assert result.success is False
    assert await storage.latest_announcement("binance") is not None
    assert any(
        state.rule_id.startswith("event.information.binance.later-risk")
        for state in await storage.list_risk_states()
    )
    health = await storage.get_collector_health("official_binance")
    assert health is not None and health.consecutive_failures == 1
    assert monitor._last_runs["binance"] == NOW


@pytest.mark.asyncio
async def test_binance_partial_health_write_failure_does_not_advance_schedule(
    storage,
    monkeypatch,
) -> None:
    partial_error = BinancePartialCollectionError(
        [],
        [("bad-detail", RuntimeError("detail request failed"))],
    )
    monitor = InformationMonitor(
        {"binance": FakeOfficialSource([partial_error])},
        storage,
        None,
        intervals={"binance": 900},
    )

    async def fail_record_health(*args, **kwargs) -> None:
        raise RuntimeError("health write failed")

    monkeypatch.setattr(scheduler_module, "_record_health", fail_record_health)

    with pytest.raises(RuntimeError, match="health write failed"):
        await monitor.check_once(now=NOW)

    assert "binance" not in monitor._last_runs


@pytest.mark.asyncio
async def test_binance_cooled_failure_does_not_report_health_recovery(
    storage,
) -> None:
    release_ms = int(NOW.timestamp() * 1000)

    class FakeHttp:
        async def get_json(self, url: str, params=None):
            if "detail/query" in url:
                return {"code": "000000", "data": {}}
            return {
                "code": "000000",
                "data": {"catalogs": [{"articles": [{
                    "code": "cooling-failure",
                    "title": "General service update",
                    "releaseDate": release_ms,
                }]}]},
            }

    collector = BinanceAnnouncementCollector(
        "https://www.binance.com/bapi/apex/v1/public/apex/cms/article/list/query",
        FakeHttp(),
        scanned_ids_provider=(
            lambda: storage.announcement_stable_ids("binance_scan")
        ),
        recent_scanned_ids_provider=(
            lambda since: storage.recent_announcement_stable_ids(
                "binance_scan", since
            )
        ),
        recent_failed_ids_provider=(
            lambda since: storage.recent_announcement_failure_ids(
                "binance_scan", since
            )
        ),
        scan_record_writer=lambda item: storage.upsert_announcement(item),
    )
    monitor = InformationMonitor(
        {"binance": collector},
        storage,
        None,
        intervals={"binance": 900},
    )

    first = await monitor.check_once(now=NOW)
    second = await monitor.check_once(now=NOW + timedelta(minutes=15))

    assert first.success is False
    assert second.success is False
    health = await storage.get_collector_health("official_binance")
    assert health is not None and health.consecutive_failures == 2


@pytest.mark.asyncio
async def test_binance_absent_cooled_failure_does_not_report_recovery(
    storage,
) -> None:
    release_ms = int(NOW.timestamp() * 1000)
    list_calls = 0

    class FakeHttp:
        async def get_json(self, url: str, params=None):
            nonlocal list_calls
            if "detail/query" in url:
                if params["articleCode"] == "gone-failure":
                    return {"code": "000000", "data": {}}
                return {
                    "code": "000000",
                    "data": {"body": "<main>ABC service update</main>"},
                }
            list_calls += 1
            stable_id = "gone-failure" if list_calls == 1 else "replacement"
            return {
                "code": "000000",
                "data": {"catalogs": [{"articles": [{
                    "code": stable_id,
                    "title": "General service update",
                    "releaseDate": release_ms,
                }]}]},
            }

    collector = BinanceAnnouncementCollector(
        "https://www.binance.com/bapi/apex/v1/public/apex/cms/article/list/query",
        FakeHttp(),
        scanned_ids_provider=(
            lambda: storage.announcement_stable_ids("binance_scan")
        ),
        recent_scanned_ids_provider=(
            lambda since: storage.recent_announcement_stable_ids(
                "binance_scan", since
            )
        ),
        recent_failed_ids_provider=(
            lambda since: storage.recent_announcement_failure_ids(
                "binance_scan", since
            )
        ),
        scan_record_writer=lambda item: storage.upsert_announcement(item),
    )
    monitor = InformationMonitor(
        {"binance": collector},
        storage,
        None,
        intervals={"binance": 900},
    )

    first = await monitor.check_once(now=NOW)
    second = await monitor.check_once(now=NOW + timedelta(minutes=15))

    assert first.success is False
    assert second.success is False
    health = await storage.get_collector_health("official_binance")
    assert health is not None and health.consecutive_failures == 2


@pytest.mark.asyncio
async def test_slow_official_source_does_not_block_other_source(storage) -> None:
    occ_started = __import__("asyncio").Event()

    class SlowSource:
        async def collect(self, collected_at):
            await occ_started.wait()
            return []

    class QuickSource:
        async def collect(self, collected_at):
            occ_started.set()
            return [occ_item()]

    monitor = InformationMonitor(
        {"binance": SlowSource(), "occ": QuickSource()},
        storage,
        None,
        intervals={"binance": 1, "occ": 1},
    )

    result = await __import__("asyncio").wait_for(
        monitor.check_once(now=NOW), timeout=0.2
    )

    assert result.success is True
    assert await storage.latest_announcement("occ") is not None


@pytest.mark.asyncio
async def test_cancelled_official_source_is_due_again(storage) -> None:
    class StuckSource:
        async def collect(self, collected_at):
            await __import__("asyncio").Event().wait()

    monitor = InformationMonitor(
        {"bitgo": StuckSource()}, storage, None, intervals={"bitgo": 21600}
    )

    with pytest.raises(TimeoutError):
        await __import__("asyncio").wait_for(
            monitor.check_once(now=NOW), timeout=0.01
        )

    assert "bitgo" not in monitor._last_runs


@pytest.mark.asyncio
async def test_unchanged_item_does_not_repeat_notification(
    storage,
) -> None:
    notifier = FakeNotifier()
    item = occ_item()
    monitor = InformationMonitor(
        {"occ": FakeOfficialSource([[item], [item]])},
        storage,
        notifier,
        intervals={"occ": 1},
    )

    await monitor.check_once(now=NOW)
    await monitor.check_once(now=NOW + timedelta(seconds=1))

    assert len(notifier.messages) == 1


@pytest.mark.asyncio
async def test_attestation_lateness_recovers_when_new_report_arrives(storage) -> None:
    monitor = InformationMonitor(
        {
            "bitgo": FakeOfficialSource(
                [[bitgo_item("2026-05")], [bitgo_item("2026-07")]]
            )
        },
        storage,
        None,
        intervals={"bitgo": 1},
    )

    await monitor.check_once(now=NOW)
    late = await storage.get_risk_state("information.attestation_late")
    assert late is not None and late.level.name == "YELLOW"

    await monitor.check_once(now=NOW + timedelta(days=3))
    current = await storage.get_risk_state("information.attestation_late")
    assert current is not None and current.level.name == "GREEN"


@pytest.mark.asyncio
async def test_latest_report_is_not_late_before_next_report_due_date(storage) -> None:
    monitor = InformationMonitor(
        {"bitgo": FakeOfficialSource([[bitgo_item("2026-07")]])},
        storage,
        None,
        intervals={"bitgo": 1},
    )

    await monitor.check_once(now=datetime(2026, 9, 15, tzinfo=UTC))

    state = await storage.get_risk_state("information.attestation_late")
    assert state is not None and state.level.name == "GREEN"


@pytest.mark.asyncio
async def test_risk_keyword_in_detail_body_is_classified(storage) -> None:
    item = detailed_item(
        "wlfi", "update", "Routine update", "USD1 custody restricted"
    )
    monitor = InformationMonitor(
        {"wlfi": FakeOfficialSource([[item]])},
        storage,
        None,
        intervals={"wlfi": 1},
    )

    await monitor.check_once(now=NOW)

    states = await storage.list_risk_states()
    assert any(
        state.rule_id.startswith("event.information.wlfi")
        and state.level.name == "YELLOW"
        for state in states
    )


@pytest.mark.asyncio
async def test_attestation_parse_failure_creates_visible_yellow(storage) -> None:
    item = Announcement(
        "bitgo", "2026-07:hash", "USD1 July attestation",
        "https://landing.bitgo.com/usd1.pdf", None, "pdfhash", NOW,
        {"parse_error": "missing auditor", "pdf_sha256": "pdfhash"},
    )
    monitor = InformationMonitor(
        {"bitgo": FakeOfficialSource([[item]])}, storage, None,
        intervals={"bitgo": 1},
    )

    await monitor.check_once(now=NOW)

    state = await storage.get_risk_state(
        "information.attestation_parse"
    )
    assert state is not None and state.level.name == "YELLOW"
    assert business_overall(await storage.list_risk_states()).name == "YELLOW"


@pytest.mark.asyncio
async def test_attestation_parse_failure_recovers_on_valid_report(storage) -> None:
    broken = Announcement(
        "bitgo", "2026-07:broken", "USD1 July attestation",
        "https://landing.bitgo.com/usd1.pdf", None, "broken", NOW,
        {"parse_error": "missing auditor", "pdf_sha256": "broken"},
    )
    valid = Announcement(
        "bitgo", "2026-08:valid", "USD1 August attestation",
        "https://landing.bitgo.com/usd1-august.pdf", None, "valid", NOW,
        {
            "report_month": "2026-08",
            "tokens_outstanding": 100.0,
            "redemption_assets": 101.0,
            "asset_categories": ["Cash"],
            "auditor": "Crowe",
            "custodian": "BitGo",
            "pdf_sha256": "valid",
        },
    )
    monitor = InformationMonitor(
        {"bitgo": FakeOfficialSource([[broken], [valid]])}, storage, None,
        intervals={"bitgo": 1},
    )

    await monitor.check_once(now=NOW)
    await monitor.check_once(now=NOW + timedelta(seconds=1))

    state = await storage.get_risk_state("information.attestation_parse")
    assert state is not None and state.level.name == "GREEN"


@pytest.mark.asyncio
async def test_latest_attestation_controls_parse_health(storage) -> None:
    latest_broken = Announcement(
        "bitgo", "2026-07:new", "USD1 July attestation",
        "https://landing.bitgo.com/july.pdf", None, "july", NOW,
        {"parse_error": "missing auditor", "pdf_sha256": "july"},
    )
    older_valid = Announcement(
        "bitgo", "2025-12:old", "USD1 December attestation",
        "https://landing.bitgo.com/december.pdf", None, "december", NOW,
        {
            "report_month": "2025-12", "tokens_outstanding": 10.0,
            "redemption_assets": 11.0, "asset_categories": ["Cash"],
            "auditor": "Crowe", "custodian": "BitGo",
            "pdf_sha256": "december",
        },
    )
    monitor = InformationMonitor(
        {"bitgo": FakeOfficialSource([[latest_broken, older_valid]])},
        storage, None, intervals={"bitgo": 1},
    )

    await monitor.check_once(now=NOW)

    state = await storage.get_risk_state("information.attestation_parse")
    assert state is not None and state.level.name == "YELLOW"


@pytest.mark.asyncio
async def test_attestation_auditor_change_creates_yellow_fact(storage) -> None:
    def report(body_hash: str, auditor: str) -> Announcement:
        return Announcement(
            "bitgo", "2026-07:report", "USD1 July attestation",
            "https://landing.bitgo.com/usd1.pdf", None, body_hash, NOW,
            {
                "report_month": "2026-07",
                "tokens_outstanding": 100.0,
                "redemption_assets": 101.0,
                "asset_categories": ["Cash"],
                "auditor": auditor,
                "custodian": "BitGo",
                "pdf_sha256": body_hash,
            },
        )

    monitor = InformationMonitor(
        {"bitgo": FakeOfficialSource([[report("one", "Crowe")], [report("two", "Other")]])},
        storage,
        None,
        intervals={"bitgo": 1},
    )

    await monitor.check_once(now=NOW)
    await monitor.check_once(now=NOW + timedelta(seconds=1))

    state = await storage.get_risk_state(
        "event.information.bitgo.attestation_fields.2026-07:report"
    )
    assert state is not None and state.level.name == "YELLOW"


@pytest.mark.asyncio
async def test_announcement_is_rolled_back_when_state_commit_fails(
    storage, monkeypatch
) -> None:
    item = detailed_item(
        "wlfi", "custody-update", "Routine update", "USD1 custody restricted"
    )
    monitor = InformationMonitor(
        {"wlfi": FakeOfficialSource([[item]])},
        storage,
        None,
        intervals={"wlfi": 1},
    )

    original_apply = StateEngine.apply_uncommitted

    async def fail_apply(self, evaluations, now):
        values = list(evaluations)
        if any(item.rule_id.startswith("event.information") for item in values):
            raise RuntimeError("state write failed")
        return await original_apply(self, values, now)

    monkeypatch.setattr(StateEngine, "apply_uncommitted", fail_apply)

    result = await monitor.check_once(now=NOW)

    assert result.success is False
    assert await storage.latest_announcement("wlfi") is None


@pytest.mark.asyncio
async def test_first_content_version_upgrade_does_not_emit_false_change(
    storage, fake_notifier
) -> None:
    legacy = detailed_item(
        "wlfi", "custody-update", "Routine update", "short index text"
    )
    legacy = Announcement(
        legacy.source, legacy.stable_id, legacy.title, legacy.url,
        legacy.published_at, legacy.body_hash, legacy.first_seen_at, {},
    )
    await storage.upsert_announcement(legacy)
    enriched = Announcement(
        legacy.source, legacy.stable_id, legacy.title, legacy.url,
        legacy.published_at, "new-body-hash", legacy.first_seen_at,
        {
            "content_version": "body-v1",
            "body_text": "USD1 custody restricted",
        },
    )
    monitor = InformationMonitor(
        {"wlfi": FakeOfficialSource([[enriched]])},
        storage,
        fake_notifier,
        intervals={"wlfi": 1},
    )

    result = await monitor.check_once(now=NOW)

    assert result.success is True
    assert fake_notifier.messages == []
    assert not any(
        state.rule_id.startswith("event.information.wlfi")
        for state in await storage.list_risk_states()
    )
