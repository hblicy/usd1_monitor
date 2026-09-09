import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from usd1_monitor.collectors.announcements import (
    Announcement,
    BinanceAnnouncementCollector,
    BinancePartialCollectionError,
    PageStructureError,
    _detail_body_text,
    content_hash,
    parse_binance_payload,
    parse_binance_items,
    parse_bitgo_items,
    parse_occ_items,
    parse_wlfi_items,
)


FIXTURES = Path(__file__).parent / "fixtures"


def test_binance_parser_returns_only_usd1_items() -> None:
    html = (FIXTURES / "binance_announcements.html").read_text(encoding="utf-8")
    items = parse_binance_items(html, "https://www.binance.com")

    assert [item.stable_id for item in items] == [
        "3dd432ca980c469389d18f2f69c9d4ae"
    ]


def test_binance_json_parser_returns_only_usd1_items() -> None:
    payload = {
        "code": "000000",
        "data": {
            "catalogs": [
                {
                    "articles": [
                        {
                            "code": "usd1-article-id",
                            "title": "Binance extends USD1 promotion",
                            "releaseDate": 1788674111414,
                        },
                        {
                            "code": "unrelated-id",
                            "title": "Binance lists ABC token",
                            "releaseDate": 1788674111414,
                        },
                    ]
                }
            ]
        },
    }

    items = parse_binance_payload(payload, "https://www.binance.com")

    assert [item.stable_id for item in items] == ["usd1-article-id"]
    assert items[0].published_at == datetime.fromtimestamp(
        1788674111414 / 1000, tz=UTC
    )


def test_binance_json_parser_rejects_missing_article_structure() -> None:
    with pytest.raises(PageStructureError, match="no announcement entries"):
        parse_binance_payload({"code": "000000", "data": {}}, "https://www.binance.com")


def test_binance_json_parser_rejects_missing_release_date() -> None:
    payload = {
        "code": "000000",
        "data": {
            "catalogs": [{
                "articles": [{
                    "code": "missing-release-date",
                    "title": "General service update",
                }]
            }]
        },
    }

    with pytest.raises(PageStructureError, match="releaseDate"):
        parse_binance_payload(
            payload,
            "https://www.binance.com",
            require_title_match=False,
        )


@pytest.mark.parametrize(
    "release_ms",
    [-1, 0, 1788674111, 1788674111414.0, 32503680000000],
)
def test_binance_json_parser_rejects_implausible_release_date(
    release_ms,
) -> None:
    payload = {
        "code": "000000",
        "data": {
            "catalogs": [{
                "articles": [{
                    "code": "invalid-release-date",
                    "title": "General service update",
                    "releaseDate": release_ms,
                }]
            }]
        },
    }

    with pytest.raises(PageStructureError, match="releaseDate"):
        parse_binance_payload(
            payload,
            "https://www.binance.com",
            require_title_match=False,
            collected_at=datetime(2026, 9, 8, 12, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_binance_collector_uses_public_json_api() -> None:
    class FakeHttp:
        calls: list[tuple[str, dict[str, object]]] = []

        async def get_json(self, url: str, params=None):
            self.calls.append((url, params))
            if "detail/query" in url:
                return {
                    "code": "000000",
                    "data": {"body": "<main>USD1 custody terms changed</main>"},
                }
            return {
                "code": "000000",
                "data": {
                    "catalogs": [
                        {
                            "articles": [
                                {
                                    "code": "usd1-id",
                                    "title": "USD1 update",
                                    "releaseDate": 1788674111414,
                                }
                            ]
                        }
                    ]
                },
            }

    http = FakeHttp()
    collected_at = datetime(2026, 9, 7, tzinfo=UTC)
    items = await BinanceAnnouncementCollector(
        "https://www.binance.com/bapi/apex/v1/public/apex/cms/article/list/query",
        http,
    ).collect(collected_at)

    assert http.calls == [
        (
            "https://www.binance.com/bapi/apex/v1/public/apex/cms/article/list/query",
            {"type": 1, "pageNo": 1, "pageSize": 20},
        ),
        (
            "https://www.binance.com/bapi/composite/v1/public/cms/article/detail/query",
            {"articleCode": "usd1-id"},
        ),
    ]
    assert items[0].first_seen_at == collected_at
    assert items[0].metadata["body_text"] == "USD1 custody terms changed"


@pytest.mark.asyncio
async def test_binance_collector_matches_usd1_in_detail_body() -> None:
    class FakeHttp:
        async def get_json(self, url: str, params=None):
            if "detail/query" in url:
                bodies = {
                    "generic-id": "<main>USD1 spot trading will be suspended</main>",
                    "unrelated-id": "<main>ABC spot trading will be suspended</main>",
                }
                return {
                    "code": "000000",
                    "data": {"body": bodies[params["articleCode"]]},
                }
            return {
                "code": "000000",
                "data": {
                    "catalogs": [{
                        "articles": [
                            {
                                "code": "generic-id",
                                "title": "Updates to Selected Spot Trading Pairs",
                                "releaseDate": 1788674111414,
                            },
                            {
                                "code": "unrelated-id",
                                "title": "Removal of Selected Spot Trading Pairs",
                                "releaseDate": 1788674111414,
                            },
                        ]
                    }]
                },
            }

    items = await BinanceAnnouncementCollector(
        "https://www.binance.com/bapi/apex/v1/public/apex/cms/article/list/query",
        FakeHttp(),
    ).collect(datetime(2026, 9, 7, tzinfo=UTC))

    assert [item.stable_id for item in items] == ["generic-id"]
    assert items[0].metadata["body_text"] == (
        "USD1 spot trading will be suspended"
    )


@pytest.mark.asyncio
async def test_binance_collector_rechecks_tracked_body_only_item(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "usd1_monitor.collectors.announcements.BINANCE_PAGE_SIZE", 1
    )

    class FakeHttp:
        async def get_json(self, url: str, params=None):
            if "detail/query" in url:
                bodies = {
                    "recent-id": "<main>ABC market update</main>",
                    "tracked-id": "<main>USD1 custody terms changed</main>",
                }
                return {
                    "code": "000000",
                    "data": {"body": bodies[params["articleCode"]]},
                }
            pages = {
                1: [{
                    "code": "recent-id",
                    "title": "Recent market update",
                    "releaseDate": 1788674111414,
                }],
                2: [{
                    "code": "tracked-id",
                    "title": "Previous market update",
                    "releaseDate": 1788674111413,
                }],
                3: [],
            }
            return {
                "code": "000000",
                "data": {"catalogs": [{"articles": pages[params["pageNo"]]}]},
            }

    async def known_ids() -> set[str]:
        return {"tracked-id"}

    items = await BinanceAnnouncementCollector(
        "https://www.binance.com/bapi/apex/v1/public/apex/cms/article/list/query",
        FakeHttp(),
        known_ids_provider=known_ids,
    ).collect(datetime(2026, 9, 7, tzinfo=UTC))

    assert [item.stable_id for item in items] == ["tracked-id"]


@pytest.mark.asyncio
async def test_binance_collector_checks_neutral_title_on_first_run() -> None:
    release_ms = 1788674111414

    class FakeHttp:
        async def get_json(self, url: str, params=None):
            if "detail/query" in url:
                return {
                    "code": "000000",
                    "data": {"body": "<main>USD1 service is restricted</main>"},
                }
            return {
                "code": "000000",
                "data": {"catalogs": [{"articles": [{
                    "code": "first-run-neutral-id",
                    "title": "General service update",
                    "releaseDate": release_ms,
                }]}]},
            }

    items = await BinanceAnnouncementCollector(
        "https://www.binance.com/bapi/apex/v1/public/apex/cms/article/list/query",
        FakeHttp(),
    ).collect(datetime.fromtimestamp(release_ms / 1000, tz=UTC))

    assert [item.stable_id for item in items] == ["first-run-neutral-id"]


@pytest.mark.asyncio
async def test_binance_collector_persists_and_resumes_body_scan_backlog(
    storage,
) -> None:
    release_ms = 1788674111414
    entries = [
        {
            "code": f"neutral-{index}",
            "title": "General service update",
            "releaseDate": release_ms - index,
        }
        for index in range(9)
    ]

    class FakeHttp:
        async def get_json(self, url: str, params=None):
            if "detail/query" in url:
                stable_id = params["articleCode"]
                body = (
                    "<main>USD1 service is restricted</main>"
                    if stable_id == "neutral-0"
                    else "<main>ABC service update</main>"
                )
                return {"code": "000000", "data": {"body": body}}
            return {
                "code": "000000",
                "data": {"catalogs": [{"articles": entries}]},
            }

    async def scanned_ids() -> set[str]:
        return await storage.announcement_stable_ids("binance_scan")

    async def recently_scanned_ids(since: datetime) -> set[str]:
        return await storage.recent_announcement_stable_ids(
            "binance_scan", since
        )

    async def record_scan(item) -> None:
        await storage.upsert_announcement(item)

    first_items = await BinanceAnnouncementCollector(
        "https://www.binance.com/bapi/apex/v1/public/apex/cms/article/list/query",
        FakeHttp(),
        scanned_ids_provider=scanned_ids,
        recent_scanned_ids_provider=recently_scanned_ids,
        scan_record_writer=record_scan,
    ).collect(datetime.fromtimestamp(release_ms / 1000, tz=UTC))
    assert first_items == []
    assert len(await scanned_ids()) == 8

    second_items = await BinanceAnnouncementCollector(
        "https://www.binance.com/bapi/apex/v1/public/apex/cms/article/list/query",
        FakeHttp(),
        scanned_ids_provider=scanned_ids,
        recent_scanned_ids_provider=recently_scanned_ids,
        scan_record_writer=record_scan,
    ).collect(datetime.fromtimestamp(release_ms / 1000, tz=UTC))

    assert [item.stable_id for item in second_items] == ["neutral-0"]
    assert second_items[0].source == "binance"


@pytest.mark.asyncio
async def test_binance_collector_rechecks_expired_neutral_scan(storage) -> None:
    release_ms = 1788674111414
    collected_at = datetime.fromtimestamp(release_ms / 1000, tz=UTC)
    body_has_usd1 = False
    detail_calls = 0

    class FakeHttp:
        async def get_json(self, url: str, params=None):
            nonlocal detail_calls
            if "detail/query" in url:
                detail_calls += 1
                body = (
                    "USD1 service is restricted"
                    if body_has_usd1
                    else "ABC service update"
                )
                return {"code": "000000", "data": {"body": f"<main>{body}</main>"}}
            return {
                "code": "000000",
                "data": {"catalogs": [{"articles": [{
                    "code": "edited-neutral-id",
                    "title": "General service update",
                    "releaseDate": release_ms,
                }]}]},
            }

    async def scanned_ids() -> set[str]:
        return await storage.announcement_stable_ids("binance_scan")

    async def recently_scanned_ids(since: datetime) -> set[str]:
        return await storage.recent_announcement_stable_ids(
            "binance_scan", since
        )

    async def record_scan(item) -> None:
        await storage.upsert_announcement(item)

    collector = BinanceAnnouncementCollector(
        "https://www.binance.com/bapi/apex/v1/public/apex/cms/article/list/query",
        FakeHttp(),
        scanned_ids_provider=scanned_ids,
        recent_scanned_ids_provider=recently_scanned_ids,
        scan_record_writer=record_scan,
    )

    assert await collector.collect(collected_at) == []
    body_has_usd1 = True
    assert await collector.collect(collected_at + timedelta(hours=23)) == []
    assert detail_calls == 1

    items = await collector.collect(collected_at + timedelta(hours=24, seconds=1))

    assert [item.stable_id for item in items] == ["edited-neutral-id"]
    assert detail_calls == 2


@pytest.mark.asyncio
async def test_binance_collector_prioritizes_never_scanned_over_expired(
    storage,
) -> None:
    collected_at = datetime(2026, 9, 8, 12, tzinfo=UTC)
    release_ms = int(collected_at.timestamp() * 1000)
    expired_ids = {f"expired-{index}" for index in range(8)}
    for index, stable_id in enumerate(sorted(expired_ids)):
        await storage.upsert_announcement(
            Announcement(
                "binance_scan",
                stable_id,
                "General service update",
                f"https://www.binance.com/en/support/announcement/{stable_id}",
                collected_at - timedelta(minutes=index),
                f"hash-{index}",
                collected_at - timedelta(hours=25),
                {"body_text": "ABC service update"},
            )
        )

    entries = [
        {
            "code": stable_id,
            "title": "General service update",
            "releaseDate": release_ms - index,
        }
        for index, stable_id in enumerate(sorted(expired_ids))
    ]
    entries.append(
        {
            "code": "never-scanned-risk",
            "title": "General service update",
            "releaseDate": release_ms - 60_000,
        }
    )

    class FakeHttp:
        async def get_json(self, url: str, params=None):
            if "detail/query" in url:
                stable_id = params["articleCode"]
                body = (
                    "USD1 withdrawals are restricted"
                    if stable_id == "never-scanned-risk"
                    else "ABC service update"
                )
                return {"code": "000000", "data": {"body": f"<main>{body}</main>"}}
            return {
                "code": "000000",
                "data": {"catalogs": [{"articles": entries}]},
            }

    async def scanned_ids() -> set[str]:
        return await storage.announcement_stable_ids("binance_scan")

    async def recently_scanned_ids(since: datetime) -> set[str]:
        return await storage.recent_announcement_stable_ids(
            "binance_scan", since
        )

    items = await BinanceAnnouncementCollector(
        "https://www.binance.com/bapi/apex/v1/public/apex/cms/article/list/query",
        FakeHttp(),
        scanned_ids_provider=scanned_ids,
        recent_scanned_ids_provider=recently_scanned_ids,
        scan_record_writer=lambda item: storage.upsert_announcement(item),
    ).collect(collected_at)

    assert [item.stable_id for item in items] == ["never-scanned-risk"]


@pytest.mark.asyncio
async def test_binance_collector_preserves_progress_after_one_detail_failure(
    storage,
) -> None:
    collected_at = datetime(2026, 9, 8, 12, tzinfo=UTC)
    release_ms = int(collected_at.timestamp() * 1000)
    entries = [
        {
            "code": "bad-detail",
            "title": "General service update",
            "releaseDate": release_ms - 60_000,
        },
        *[
            {
                "code": f"neutral-{index}",
                "title": "General service update",
                "releaseDate": release_ms - index - 1,
            }
            for index in range(7)
        ],
        {
            "code": "later-risk",
            "title": "General service update",
            "releaseDate": release_ms,
        },
    ]

    class FakeHttp:
        async def get_json(self, url: str, params=None):
            if "detail/query" in url:
                stable_id = params["articleCode"]
                if stable_id == "bad-detail":
                    return {"code": "000000", "data": {}}
                body = (
                    "USD1 withdrawals are restricted"
                    if stable_id == "later-risk"
                    else "ABC service update"
                )
                return {"code": "000000", "data": {"body": f"<main>{body}</main>"}}
            return {
                "code": "000000",
                "data": {"catalogs": [{"articles": entries}]},
            }

    async def scanned_ids() -> set[str]:
        return await storage.announcement_stable_ids("binance_scan")

    async def recently_scanned_ids(since: datetime) -> set[str]:
        return await storage.recent_announcement_stable_ids(
            "binance_scan", since
        )

    collector = BinanceAnnouncementCollector(
        "https://www.binance.com/bapi/apex/v1/public/apex/cms/article/list/query",
        FakeHttp(),
        scanned_ids_provider=scanned_ids,
        recent_scanned_ids_provider=recently_scanned_ids,
        scan_record_writer=lambda item: storage.upsert_announcement(item),
    )

    with pytest.raises(BinancePartialCollectionError) as first_error:
        await collector.collect(collected_at)

    assert first_error.value.items == []
    assert await scanned_ids() == {
        "bad-detail",
        *(f"neutral-{index}" for index in range(7)),
    }

    second_items = await collector.collect(
        collected_at + timedelta(minutes=15)
    )

    assert [item.stable_id for item in second_items] == ["later-risk"]


@pytest.mark.asyncio
async def test_binance_failed_first_scan_batch_cools_down_before_newer_risk(
    storage,
) -> None:
    collected_at = datetime(2026, 9, 8, 12, tzinfo=UTC)
    release_ms = int(collected_at.timestamp() * 1000)
    failed_ids = {f"failed-{index}" for index in range(8)}
    entries = [
        {
            "code": "newer-risk",
            "title": "General service update",
            "releaseDate": release_ms,
        },
        *[
            {
                "code": stable_id,
                "title": "General service update",
                "releaseDate": release_ms - 60_000 - index,
            }
            for index, stable_id in enumerate(sorted(failed_ids))
        ],
    ]

    class FakeHttp:
        async def get_json(self, url: str, params=None):
            if "detail/query" in url:
                stable_id = params["articleCode"]
                if stable_id in failed_ids:
                    return {"code": "000000", "data": {}}
                return {
                    "code": "000000",
                    "data": {"body": "<main>USD1 withdrawals are restricted</main>"},
                }
            return {
                "code": "000000",
                "data": {"catalogs": [{"articles": entries}]},
            }

    async def scanned_ids() -> set[str]:
        return await storage.announcement_stable_ids("binance_scan")

    async def recently_scanned_ids(since: datetime) -> set[str]:
        return await storage.recent_announcement_stable_ids(
            "binance_scan", since
        )

    collector = BinanceAnnouncementCollector(
        "https://www.binance.com/bapi/apex/v1/public/apex/cms/article/list/query",
        FakeHttp(),
        scanned_ids_provider=scanned_ids,
        recent_scanned_ids_provider=recently_scanned_ids,
        scan_record_writer=lambda item: storage.upsert_announcement(item),
    )

    with pytest.raises(BinancePartialCollectionError) as first_error:
        await collector.collect(collected_at)

    assert first_error.value.items == []
    assert await scanned_ids() == failed_ids

    second_items = await collector.collect(collected_at + timedelta(minutes=15))

    assert [item.stable_id for item in second_items] == ["newer-risk"]


@pytest.mark.asyncio
async def test_binance_slow_detail_does_not_block_other_detail_results(
    storage,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "usd1_monitor.collectors.announcements.BINANCE_DETAIL_TIMEOUT_SECONDS",
        0.01,
        raising=False,
    )
    collected_at = datetime(2026, 9, 8, 12, tzinfo=UTC)
    release_ms = int(collected_at.timestamp() * 1000)
    entries = [
        {
            "code": "slow-detail",
            "title": "General service update",
            "releaseDate": release_ms - 1,
        },
        {
            "code": "concurrent-risk",
            "title": "General service update",
            "releaseDate": release_ms,
        },
    ]

    class FakeHttp:
        async def get_json(self, url: str, params=None):
            if "detail/query" not in url:
                return {
                    "code": "000000",
                    "data": {"catalogs": [{"articles": entries}]},
                }
            if params["articleCode"] == "slow-detail":
                await asyncio.Event().wait()
            return {
                "code": "000000",
                "data": {"body": "<main>USD1 withdrawals are restricted</main>"},
            }

    async def scanned_ids() -> set[str]:
        return await storage.announcement_stable_ids("binance_scan")

    collector = BinanceAnnouncementCollector(
        "https://www.binance.com/bapi/apex/v1/public/apex/cms/article/list/query",
        FakeHttp(),
        scanned_ids_provider=scanned_ids,
        recent_scanned_ids_provider=(
            lambda since: storage.recent_announcement_stable_ids(
                "binance_scan", since
            )
        ),
        scan_record_writer=lambda item: storage.upsert_announcement(item),
    )

    with pytest.raises(BinancePartialCollectionError) as error:
        await asyncio.wait_for(collector.collect(collected_at), timeout=0.2)

    assert [item.stable_id for item in error.value.items] == [
        "concurrent-risk"
    ]
    assert await scanned_ids() == {"slow-detail"}


@pytest.mark.asyncio
async def test_binance_title_details_do_not_starve_body_scan_candidate(
    storage,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "usd1_monitor.collectors.announcements.BINANCE_DETAIL_CONCURRENCY",
        2,
    )
    monkeypatch.setattr(
        "usd1_monitor.collectors.announcements.BINANCE_DETAIL_TIMEOUT_SECONDS",
        0.01,
    )
    collected_at = datetime(2026, 9, 8, 12, tzinfo=UTC)
    release_ms = int(collected_at.timestamp() * 1000)
    entries = [
        {
            "code": f"slow-title-{index}",
            "title": f"USD1 service update {index}",
            "releaseDate": release_ms - index,
        }
        for index in range(2)
    ]
    entries.append(
        {
            "code": "body-only-risk",
            "title": "General service update",
            "releaseDate": release_ms - 60_000,
        }
    )

    class FakeHttp:
        async def get_json(self, url: str, params=None):
            if "detail/query" not in url:
                return {
                    "code": "000000",
                    "data": {"catalogs": [{"articles": entries}]},
                }
            if params["articleCode"].startswith("slow-title-"):
                await asyncio.Event().wait()
            return {
                "code": "000000",
                "data": {
                    "body": "<main>USD1 withdrawals are restricted</main>"
                },
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
        scan_record_writer=lambda item: storage.upsert_announcement(item),
    )

    with pytest.raises(BinancePartialCollectionError) as error:
        await asyncio.wait_for(collector.collect(collected_at), timeout=0.2)

    assert [item.stable_id for item in error.value.items] == [
        "body-only-risk"
    ]


@pytest.mark.asyncio
async def test_binance_title_match_respects_detail_failure_cooldown(
    storage,
) -> None:
    collected_at = datetime(2026, 9, 8, 12, tzinfo=UTC)
    release_ms = int(collected_at.timestamp() * 1000)
    detail_calls = 0

    class FakeHttp:
        async def get_json(self, url: str, params=None):
            nonlocal detail_calls
            if "detail/query" in url:
                detail_calls += 1
                return {"code": "000000", "data": {}}
            return {
                "code": "000000",
                "data": {"catalogs": [{"articles": [{
                    "code": "title-match-failure",
                    "title": "USD1 service update",
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

    with pytest.raises(BinancePartialCollectionError):
        await collector.collect(collected_at)
    with pytest.raises(BinancePartialCollectionError):
        await collector.collect(collected_at + timedelta(minutes=15))

    assert detail_calls == 1


@pytest.mark.asyncio
async def test_binance_scan_record_writer_failure_propagates() -> None:
    release_ms = 1788674111414

    class FakeHttp:
        async def get_json(self, url: str, params=None):
            if "detail/query" in url:
                return {
                    "code": "000000",
                    "data": {"body": "<main>ABC service update</main>"},
                }
            return {
                "code": "000000",
                "data": {"catalogs": [{"articles": [{
                    "code": "neutral-write-failure",
                    "title": "General service update",
                    "releaseDate": release_ms,
                }]}]},
            }

    async def fail_scan_write(item) -> None:
        raise RuntimeError("scan record write failed")

    collector = BinanceAnnouncementCollector(
        "https://www.binance.com/bapi/apex/v1/public/apex/cms/article/list/query",
        FakeHttp(),
        scan_record_writer=fail_scan_write,
    )

    with pytest.raises(RuntimeError, match="scan record write failed"):
        await collector.collect(datetime.fromtimestamp(release_ms / 1000, tz=UTC))


@pytest.mark.asyncio
async def test_binance_scan_record_write_failure_is_not_partial_failure() -> None:
    release_ms = 1788674111414

    class FakeHttp:
        async def get_json(self, url: str, params=None):
            if "detail/query" in url:
                return {
                    "code": "000000",
                    "data": {"body": "<main>ABC service update</main>"},
                }
            return {
                "code": "000000",
                "data": {"catalogs": [{"articles": [{
                    "code": "neutral-write-failure",
                    "title": "General service update",
                    "releaseDate": release_ms,
                }]}]},
            }

    async def fail_scan_write(item) -> None:
        raise RuntimeError("scan record write failed")

    collector = BinanceAnnouncementCollector(
        "https://www.binance.com/bapi/apex/v1/public/apex/cms/article/list/query",
        FakeHttp(),
        scan_record_writer=fail_scan_write,
    )

    with pytest.raises(RuntimeError, match="scan record write failed"):
        await collector.collect(datetime.fromtimestamp(release_ms / 1000, tz=UTC))


def test_empty_official_page_is_parser_failure() -> None:
    with pytest.raises(PageStructureError, match="no announcement entries"):
        parse_binance_items("<html><body></body></html>", "https://www.binance.com")


def test_other_official_adapters_extract_stable_ids() -> None:
    bitgo = parse_bitgo_items(
        (FIXTURES / "bitgo_attestations.html").read_text(encoding="utf-8"),
        "https://www.bitgo.com",
    )
    wlfi = parse_wlfi_items(
        (FIXTURES / "wlfi_por.html").read_text(encoding="utf-8"),
        "https://docs.worldlibertyfinancial.com",
    )
    occ = parse_occ_items(
        (FIXTURES / "occ_decisions.html").read_text(encoding="utf-8"),
        "https://www.occ.gov",
    )

    assert bitgo[0].stable_id.startswith("2026-07:")
    assert wlfi[0].stable_id == "/usd1-token/proof-of-reserves"
    assert occ[0].stable_id == "1385"


def test_occ_parser_matches_entity_in_table_description() -> None:
    html = """
    <table><tr>
      <td><a href="/topics/charters-and-licensing/interpretations-and-decisions/2026/cd1385.pdf">Corporate Decision 1385</a></td>
      <td>August 2026</td><td>08/14/2026</td>
      <td>Application to charter World Liberty Trust Company, National Association</td>
    </tr></table>
    """

    items = parse_occ_items(html, "https://www.occ.gov")

    assert [item.stable_id for item in items] == ["1385"]
    assert "World Liberty" in items[0].title


def test_bitgo_parser_ignores_navigation_and_reads_named_month() -> None:
    html = """
    <a href="/stablecoin-reserves/">Attestations</a>
    <a href="https://landing.bitgo.com/guide.pdf?version=0">Custody Guide</a>
    <a href="https://landing.bitgo.com/USD1_Reserve_Attestation_Report_July_2026.pdf?version=0">Published</a>
    """

    items = parse_bitgo_items(html, "https://www.bitgo.com/usd1/attestations/")

    assert len(items) == 1
    assert items[0].stable_id.startswith("2026-07:")


def test_bitgo_parser_orders_reports_by_month() -> None:
    html = """
    <a href="https://landing.bitgo.com/USD1_Attestation_July_2026.pdf">July</a>
    <a href="https://landing.bitgo.com/USD1_Attestation_April_2025.pdf">April</a>
    <a href="https://landing.bitgo.com/USD1_Attestation_August_2026.pdf">August</a>
    """

    items = parse_bitgo_items(html, "https://www.bitgo.com/usd1/attestations/")

    assert [item.stable_id[:7] for item in items] == [
        "2025-04", "2026-07", "2026-08"
    ]


def test_external_pdf_host_is_rejected() -> None:
    with pytest.raises(PageStructureError, match="outside allowlist"):
        parse_bitgo_items(
            '<a href="https://evil.example/2026-07.pdf">USD1 July 2026 Attestation</a>',
            "https://www.bitgo.com",
        )


@pytest.mark.parametrize(
    "href",
    [
        "http://landing.bitgo.com/USD1_Attestation_July_2026.pdf",
        "https://user:pass@landing.bitgo.com/USD1_Attestation_July_2026.pdf",
        "https://landing.bitgo.com:444/USD1_Attestation_July_2026.pdf",
    ],
)
def test_official_links_require_strict_https(href: str) -> None:
    with pytest.raises(PageStructureError):
        parse_bitgo_items(
            f'<a href="{href}">USD1 July 2026 Attestation</a>',
            "https://www.bitgo.com",
        )


def test_wlfi_official_por_subdomain_is_allowed() -> None:
    items = parse_wlfi_items(
        '<a href="https://por.worldlibertyfinancial.com/">USD1 Proof of Reserves</a>',
        "https://docs.worldlibertyfinancial.com/usd1-token/proof-of-reserves",
    )

    assert items[0].url == "https://por.worldlibertyfinancial.com/"


def test_wlfi_parser_skips_external_usd1_links() -> None:
    html = """
    <a href="https://www.bitgo.com/usd1-attestations/">USD1 attestations</a>
    <a href="/usd1-token/proof-of-reserves">USD1 Proof of Reserves</a>
    """

    items = parse_wlfi_items(
        html,
        "https://docs.worldlibertyfinancial.com/usd1-token/proof-of-reserves",
    )

    assert [item.url for item in items] == [
        "https://docs.worldlibertyfinancial.com/usd1-token/proof-of-reserves"
    ]


def test_wlfi_parser_reads_current_gitbook_markdown_links() -> None:
    markdown = """
    # Proof of Reserves

    * [USD1 Attestation Reports](/usd1-token/attestation-reports.md)
    * [BitGo USD1 Attestations](https://www.bitgo.com/usd1-attestations/)
    """

    items = parse_wlfi_items(
        markdown,
        "https://docs.worldlibertyfinancial.com/usd1-token/proof-of-reserves",
    )

    assert [(item.title, item.url) for item in items] == [
        (
            "USD1 Attestation Reports",
            "https://docs.worldlibertyfinancial.com/usd1-token/attestation-reports.md",
        )
    ]


@pytest.mark.asyncio
async def test_official_collector_hashes_detail_body_not_title_and_url() -> None:
    class DetailHttp:
        def __init__(self) -> None:
            self.calls = []

        async def get_text(self, url: str) -> str:
            self.calls.append(url)
            if len(self.calls) == 1:
                return '<a href="/usd1-update">USD1 reserve update</a>'
            return '<main><h1>USD1 reserve update</h1><p>Custodian changed.</p></main>'

    from usd1_monitor.collectors.announcements import OfficialPageCollector, content_hash

    http = DetailHttp()
    item = (
        await OfficialPageCollector(
            "wlfi",
            "https://docs.worldlibertyfinancial.com/index",
            http,
            parse_wlfi_items,
        ).collect(datetime(2026, 9, 7, tzinfo=UTC))
    )[0]

    assert http.calls == [
        "https://docs.worldlibertyfinancial.com/index",
        "https://docs.worldlibertyfinancial.com/usd1-update",
    ]
    assert item.body_hash == content_hash(
        "USD1 reserve update", "USD1 reserve update Custodian changed."
    )
    assert item.metadata["body_text"] == "USD1 reserve update Custodian changed."
    assert item.metadata["content_version"] == "body-v1"


@pytest.mark.asyncio
async def test_official_detail_hash_ignores_navigation_and_footer_changes() -> None:
    from usd1_monitor.collectors.announcements import OfficialPageCollector

    class DetailHttp:
        def __init__(self, shell_version: str) -> None:
            self.shell_version = shell_version
            self.calls = 0

        async def get_text(self, url: str) -> str:
            self.calls += 1
            if self.calls == 1:
                return '<a href="/usd1-update">USD1 reserve update</a>'
            return (
                f"<nav>{self.shell_version}</nav>"
                "<main><h1>USD1 reserve update</h1><p>Custodian unchanged.</p></main>"
                f"<footer>{self.shell_version}</footer>"
            )

    async def collect_hash(shell_version: str) -> str:
        item = (
            await OfficialPageCollector(
                "wlfi",
                "https://docs.worldlibertyfinancial.com/index",
                DetailHttp(shell_version),
                parse_wlfi_items,
            ).collect(datetime(2026, 9, 7, tzinfo=UTC))
        )[0]
        return item.body_hash

    assert await collect_hash("navigation-v1") == await collect_hash("navigation-v2")


@pytest.mark.asyncio
async def test_official_detail_requires_main_or_article_content() -> None:
    from usd1_monitor.collectors.announcements import OfficialPageCollector

    class DetailHttp:
        calls = 0

        async def get_text(self, url: str) -> str:
            self.calls += 1
            if self.calls == 1:
                return '<a href="/usd1-update">USD1 reserve update</a>'
            return '<span class="site-shell">USD1 reserve update</span>'

    with pytest.raises(PageStructureError, match="main/article"):
        await OfficialPageCollector(
            "wlfi",
            "https://docs.worldlibertyfinancial.com/index",
            DetailHttp(),
            parse_wlfi_items,
        ).collect(datetime(2026, 9, 7, tzinfo=UTC))


@pytest.mark.asyncio
async def test_official_detail_accepts_plain_markdown_body() -> None:
    from usd1_monitor.collectors.announcements import OfficialPageCollector

    class DetailHttp:
        calls = 0

        async def get_text(self, url: str) -> str:
            self.calls += 1
            if self.calls == 1:
                return '<a href="/usd1-update">USD1 reserve update</a>'
            return "# USD1 reserve update\n\nCustodian unchanged."

    item = (
        await OfficialPageCollector(
            "wlfi",
            "https://docs.worldlibertyfinancial.com/index",
            DetailHttp(),
            parse_wlfi_items,
        ).collect(datetime(2026, 9, 7, tzinfo=UTC))
    )[0]

    assert item.metadata["body_text"] == (
        "# USD1 reserve update Custodian unchanged."
    )


def test_gitbook_agent_instructions_tail_does_not_change_detail_hash() -> None:
    body = "# Proof of Reserves\n\nUSD1 reserves are unchanged."
    first = body + "\n\n---\n\n# Agent Instructions\n\nglobal-v1"
    second = (
        body + "\n\n---\n\n# Agent Instructions\n\nglobal-v2"
    ).replace("\n", "\r\n")

    first_body = _detail_body_text(first)
    second_body = _detail_body_text(second)

    assert first_body == "# Proof of Reserves USD1 reserves are unchanged."
    assert content_hash("Proof of Reserves", first_body) == content_hash(
        "Proof of Reserves", second_body
    )


@pytest.mark.asyncio
async def test_official_detail_accepts_markdown_with_embedded_html() -> None:
    from usd1_monitor.collectors.announcements import OfficialPageCollector

    class DetailHttp:
        calls = 0

        async def get_text(self, url: str) -> str:
            self.calls += 1
            if self.calls == 1:
                return '<a href="/usd1-update">USD1 reserve update</a>'
            return (
                "> For the complete documentation index, see llms.txt.\n\n"
                "# USD1 reserve update\n\n"
                "<figure><img src=\"/proof.png\" alt=\"\"></figure>"
            )

    item = (
        await OfficialPageCollector(
            "wlfi",
            "https://docs.worldlibertyfinancial.com/index",
            DetailHttp(),
            parse_wlfi_items,
        ).collect(datetime(2026, 9, 7, tzinfo=UTC))
    )[0]

    assert "# USD1 reserve update" in item.metadata["body_text"]


@pytest.mark.asyncio
async def test_bitgo_collector_hashes_pdf_and_exposes_attestation(monkeypatch) -> None:
    from usd1_monitor.collectors import announcements
    from usd1_monitor.collectors.announcements import OfficialPageCollector
    from usd1_monitor.collectors.attestations import AttestationReport, pdf_sha256

    pdf = b"real pdf bytes"

    class PdfHttp:
        async def get_text(self, url: str) -> str:
            return (
                '<a href="https://landing.bitgo.com/'
                'USD1_Reserve_Attestation_Report_July_2026.pdf">Published</a>'
            )

        async def get_bytes(self, url: str) -> bytes:
            return pdf

    monkeypatch.setattr(
        announcements,
        "extract_attestation",
        lambda value: AttestationReport(
            "2026-07", 100.0, 101.0, ("Cash",), "Crowe", "BitGo"
        ),
    )
    item = (
        await OfficialPageCollector(
            "bitgo",
            "https://www.bitgo.com/usd1/attestations/",
            PdfHttp(),
            parse_bitgo_items,
        ).collect(datetime(2026, 9, 7, tzinfo=UTC))
    )[0]

    assert item.body_hash == pdf_sha256(pdf)
    assert item.metadata["report_month"] == "2026-07"
    assert item.metadata["auditor"] == "Crowe"
    assert item.metadata["content_version"] == "pdf-v1"


@pytest.mark.asyncio
async def test_bitgo_collector_downloads_only_latest_report(monkeypatch) -> None:
    from usd1_monitor.collectors import announcements
    from usd1_monitor.collectors.announcements import OfficialPageCollector
    from usd1_monitor.collectors.attestations import AttestationReport

    class PdfHttp:
        requested: list[str] = []

        async def get_text(self, url: str) -> str:
            return """
            <a href="https://landing.bitgo.com/USD1_Attestation_July_2026.pdf">July</a>
            <a href="https://landing.bitgo.com/USD1_Attestation_August_2026.pdf">August</a>
            """

        async def get_bytes(self, url: str) -> bytes:
            self.requested.append(url)
            return b"pdf"

    monkeypatch.setattr(
        announcements,
        "extract_attestation",
        lambda value: AttestationReport(
            "2026-08", 100.0, 101.0, ("Cash",), "Crowe", "BitGo"
        ),
    )
    http = PdfHttp()

    items = await OfficialPageCollector(
        "bitgo", "https://www.bitgo.com/usd1/attestations/",
        http, parse_bitgo_items,
    ).collect(datetime(2026, 9, 7, tzinfo=UTC))

    assert len(items) == 1
    assert items[0].stable_id.startswith("2026-08:")
    assert len(http.requested) == 1


@pytest.mark.asyncio
async def test_bitgo_collector_prefers_explicit_same_month_revision(monkeypatch) -> None:
    from usd1_monitor.collectors import announcements
    from usd1_monitor.collectors.announcements import OfficialPageCollector
    from usd1_monitor.collectors.attestations import AttestationReport

    class PdfHttp:
        requested: list[str] = []

        async def get_text(self, url: str) -> str:
            return """
            <a href="https://landing.bitgo.com/USD1_Attestation_July_2026.pdf">Original</a>
            <a href="https://landing.bitgo.com/USD1_Attestation_July_2026_Revised.pdf">Revised</a>
            """

        async def get_bytes(self, url: str) -> bytes:
            self.requested.append(url)
            return b"pdf"

    monkeypatch.setattr(
        announcements,
        "extract_attestation",
        lambda value: AttestationReport(
            "2026-07", 100.0, 101.0, ("Cash",), "Crowe", "BitGo"
        ),
    )
    http = PdfHttp()

    await OfficialPageCollector(
        "bitgo",
        "https://www.bitgo.com/usd1/attestations/",
        http,
        parse_bitgo_items,
    ).collect(datetime(2026, 9, 7, tzinfo=UTC))

    assert len(http.requested) == 1
    assert "Revised" in http.requested[0]


@pytest.mark.asyncio
async def test_bitgo_same_month_tie_uses_page_order(monkeypatch) -> None:
    from usd1_monitor.collectors import announcements
    from usd1_monitor.collectors.announcements import OfficialPageCollector
    from usd1_monitor.collectors.attestations import AttestationReport

    class PdfHttp:
        requested: list[str] = []

        async def get_text(self, url: str) -> str:
            return """
            <a href="https://landing.bitgo.com/USD1_Attestation_August_2026_B.pdf">Latest</a>
            <a href="https://landing.bitgo.com/USD1_Attestation_August_2026_A.pdf">Older</a>
            """

        async def get_bytes(self, url: str) -> bytes:
            self.requested.append(url)
            return b"pdf"

    monkeypatch.setattr(
        announcements,
        "extract_attestation",
        lambda value: AttestationReport(
            "2026-08", 100.0, 101.0, ("Cash",), "Crowe", "BitGo"
        ),
    )
    http = PdfHttp()

    await OfficialPageCollector(
        "bitgo",
        "https://www.bitgo.com/usd1/attestations/",
        http,
        parse_bitgo_items,
    ).collect(datetime(2026, 9, 7, tzinfo=UTC))

    assert http.requested == [
        "https://landing.bitgo.com/USD1_Attestation_August_2026_B.pdf"
    ]


@pytest.mark.asyncio
async def test_bitgo_link_month_must_match_pdf_report_month(monkeypatch) -> None:
    from usd1_monitor.collectors import announcements
    from usd1_monitor.collectors.announcements import OfficialPageCollector
    from usd1_monitor.collectors.attestations import AttestationReport

    class PdfHttp:
        async def get_text(self, url: str) -> str:
            return (
                '<a href="https://landing.bitgo.com/'
                'USD1_Attestation_August_2026.pdf">USD1 August 2026</a>'
            )

        async def get_bytes(self, url: str) -> bytes:
            return b"pdf"

    monkeypatch.setattr(
        announcements,
        "extract_attestation",
        lambda value: AttestationReport(
            "2026-07", 100.0, 101.0, ("Cash",), "Crowe", "BitGo"
        ),
    )

    with pytest.raises(PageStructureError, match="report month mismatch"):
        await OfficialPageCollector(
            "bitgo",
            "https://www.bitgo.com/usd1/attestations/",
            PdfHttp(),
            parse_bitgo_items,
        ).collect(datetime(2026, 9, 7, tzinfo=UTC))


@pytest.mark.asyncio
async def test_pdf_parsing_runs_outside_event_loop_thread(monkeypatch) -> None:
    import threading

    from usd1_monitor.collectors import announcements
    from usd1_monitor.collectors.announcements import OfficialPageCollector
    from usd1_monitor.collectors.attestations import AttestationReport

    main_thread = threading.get_ident()
    parser_threads: list[int] = []

    class PdfHttp:
        async def get_text(self, url: str) -> str:
            return (
                '<a href="https://landing.bitgo.com/'
                'USD1_Attestation_July_2026.pdf">USD1 Attestation July 2026</a>'
            )

        async def get_bytes(self, url: str) -> bytes:
            return b"pdf"

    def parse_pdf(value):
        parser_threads.append(threading.get_ident())
        return AttestationReport(
            "2026-07", 100.0, 101.0, ("Cash",), "Crowe", "BitGo"
        )

    monkeypatch.setattr(announcements, "extract_attestation", parse_pdf)

    await OfficialPageCollector(
        "bitgo",
        "https://www.bitgo.com/usd1/attestations/",
        PdfHttp(),
        parse_bitgo_items,
    ).collect(datetime(2026, 9, 7, tzinfo=UTC))

    assert parser_threads and parser_threads[0] != main_thread


@pytest.mark.asyncio
async def test_binance_collector_reads_later_pages() -> None:
    class PagedHttp:
        calls: list[tuple[str, dict[str, object]]] = []

        async def get_json(self, url: str, params=None):
            self.calls.append((url, params))
            if "detail/query" in url:
                body = (
                    "USD1 update"
                    if params["articleCode"] == "usd1-page-2"
                    else "Other update"
                )
                return {"code": "000000", "data": {"body": body}}
            page = params["pageNo"]
            if page == 1:
                articles = [
                    {
                        "code": f"other-{index}",
                        "title": f"Other {index}",
                        "releaseDate": 1788674111414 - index,
                    }
                    for index in range(20)
                ]
            else:
                articles = [{
                    "code": "usd1-page-2",
                    "title": "USD1 update",
                    "releaseDate": 1788674111394,
                }]
            return {"code": "000000", "data": {"catalogs": [{"articles": articles}]}}

    http = PagedHttp()
    items = await BinanceAnnouncementCollector(
        "https://www.binance.com/bapi/apex/v1/public/apex/cms/article/list/query",
        http,
    ).collect(datetime(2026, 9, 7, tzinfo=UTC))

    assert [item.stable_id for item in items] == ["usd1-page-2"]
    list_pages = [
        params["pageNo"] for url, params in http.calls if "list/query" in url
    ]
    assert list_pages == [1, 2]


@pytest.mark.asyncio
async def test_binance_collector_stops_when_endpoint_repeats_page() -> None:
    class RepeatingHttp:
        calls: list[tuple[str, dict[str, object]]] = []

        async def get_json(self, url: str, params=None):
            self.calls.append((url, params))
            if "detail/query" in url:
                return {"code": "000000", "data": {"body": "Other update"}}
            articles = [
                {
                    "code": f"other-{index}",
                    "title": f"Other {index}",
                    "releaseDate": 1788674111414 - index,
                }
                for index in range(20)
            ]
            return {"code": "000000", "data": {"catalogs": [{"articles": articles}]}}

    http = RepeatingHttp()

    items = await BinanceAnnouncementCollector(
        "https://www.binance.com/bapi/apex/v1/public/apex/cms/article/list/query",
        http,
    ).collect(datetime(2026, 9, 7, tzinfo=UTC))

    assert items == []
    assert len([url for url, _ in http.calls if "list/query" in url]) == 2


@pytest.mark.asyncio
async def test_occ_collector_reads_pdf_bytes_and_extracts_text(monkeypatch) -> None:
    from usd1_monitor.collectors import announcements
    from usd1_monitor.collectors.announcements import OfficialPageCollector, content_hash

    pdf = b"occ pdf bytes"

    class PdfHttp:
        async def get_text(self, url: str) -> str:
            return """
            <table><tr>
              <td><a href="/2026/cd1385.pdf">Corporate Decision 1385</a></td>
              <td>Application to charter World Liberty Trust Company</td>
            </tr></table>
            """

        async def get_bytes(self, url: str) -> bytes:
            return pdf

    monkeypatch.setattr(
        announcements,
        "extract_pdf_text",
        lambda value: "USD1 charter approval conditions changed",
        raising=False,
    )
    item = (
        await OfficialPageCollector(
            "occ",
            "https://www.occ.gov/index.html",
            PdfHttp(),
            parse_occ_items,
        ).collect(datetime(2026, 9, 7, tzinfo=UTC))
    )[0]

    assert item.metadata["body_text"] == "USD1 charter approval conditions changed"
    assert item.body_hash == content_hash(
        item.title, "USD1 charter approval conditions changed"
    )
