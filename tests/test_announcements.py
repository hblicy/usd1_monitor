from datetime import UTC, datetime

import pytest

from usd1_monitor.collectors.announcements import (
    Announcement,
    content_hash,
    matches_usd1,
    normalize_text,
)


def test_world_liberty_usd1_matches() -> None:
    assert matches_usd1(
        "Binance changes World Liberty Financial USD (USD1) margin support"
    ) is True


def test_unitas_usd1_is_excluded() -> None:
    assert matches_usd1("Unitas USD1 launches on a new network") is False


def test_normalize_text_removes_layout_noise() -> None:
    assert normalize_text("  USD1\n\n reserve\t update  ") == "USD1 reserve update"


def item(body: str = "reserve update") -> Announcement:
    return Announcement(
        source="bitgo",
        stable_id="2026-07",
        title="USD1 July 2026 attestation",
        url="https://www.bitgo.com/usd1/attestations/",
        published_at=None,
        body_hash=content_hash("USD1 July 2026 attestation", body),
        first_seen_at=datetime(2026, 9, 7, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_same_id_and_hash_is_not_inserted_twice(storage) -> None:
    assert await storage.upsert_announcement(item()) == "NEW"
    assert await storage.upsert_announcement(item()) == "UNCHANGED"


@pytest.mark.asyncio
async def test_changed_hash_is_reported_once_and_preserves_first_seen(storage) -> None:
    original = item()
    changed = item("custodian changed")

    assert await storage.upsert_announcement(original) == "NEW"
    assert await storage.upsert_announcement(changed) == "CHANGED"
    assert await storage.upsert_announcement(changed) == "UNCHANGED"
    stored = await storage.latest_announcement("bitgo")
    assert stored.first_seen_at == original.first_seen_at


@pytest.mark.asyncio
async def test_first_enriched_hash_update_is_baselined(storage) -> None:
    legacy = item()
    enriched = Announcement(
        legacy.source,
        legacy.stable_id,
        legacy.title,
        legacy.url,
        legacy.published_at,
        content_hash(legacy.title, "full body"),
        legacy.first_seen_at,
        {"content_version": "body-v1", "body_text": "full body"},
    )
    revised = Announcement(
        enriched.source,
        enriched.stable_id,
        enriched.title,
        enriched.url,
        enriched.published_at,
        content_hash(enriched.title, "revised body"),
        enriched.first_seen_at,
        {"content_version": "body-v1", "body_text": "revised body"},
    )

    assert await storage.upsert_announcement(legacy) == "NEW"
    assert await storage.upsert_announcement(enriched) == "BASELINED"
    assert await storage.upsert_announcement(revised) == "CHANGED"


@pytest.mark.asyncio
async def test_latest_bitgo_announcement_uses_report_month(storage) -> None:
    for month in ("2026-07", "2025-12"):
        report = item()
        report = Announcement(
            report.source,
            f"{month}:hash",
            report.title,
            report.url,
            report.published_at,
            report.body_hash,
            report.first_seen_at,
        )
        await storage.upsert_announcement(report)

    latest = await storage.latest_announcement("bitgo")

    assert latest is not None
    assert latest.stable_id.startswith("2026-07:")
