import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from usd1_monitor.engine.state import StateEngine
from usd1_monitor.models import (
    Announcement,
    ChainEvent,
    Observation,
    RiskLevel,
    RuleEvaluation,
)
from usd1_monitor.storage import Storage


@pytest.mark.asyncio
async def test_announcement_stable_ids_are_scoped_by_source(storage) -> None:
    now = datetime.now(UTC)
    for source, stable_id in (
        ("binance", "first"),
        ("binance", "second"),
        ("occ", "other"),
    ):
        await storage.upsert_announcement(
            Announcement(
                source,
                stable_id,
                "USD1 update",
                f"https://example.com/{stable_id}",
                now,
                stable_id,
                now,
            )
        )

    assert await storage.announcement_stable_ids("binance") == {
        "first", "second"
    }


@pytest.mark.asyncio
async def test_recent_announcement_failure_ids_filters_status_age_and_source(
    storage,
) -> None:
    now = datetime(2026, 9, 9, 12, tzinfo=UTC)
    for source, stable_id, seen_at, metadata in (
        ("binance_scan", "recent-failure", now, {"scan_error": "TimeoutError"}),
        ("binance_scan", "recent-success", now, {"usd1_relevant": False}),
        (
            "binance_scan",
            "expired-failure",
            now - timedelta(hours=25),
            {"scan_error": "TimeoutError"},
        ),
        ("binance", "other-source", now, {"scan_error": "TimeoutError"}),
    ):
        await storage.upsert_announcement(
            Announcement(
                source,
                stable_id,
                "General service update",
                f"https://www.binance.com/{stable_id}",
                now,
                stable_id,
                seen_at,
                metadata,
            )
        )

    assert await storage.recent_announcement_failure_ids(
        "binance_scan", now - timedelta(hours=24)
    ) == {"recent-failure"}


@pytest.mark.asyncio
async def test_observation_and_risk_state_survive_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "monitor.db"
    observed_at = datetime(2026, 9, 7, 4, 0, tzinfo=UTC)
    storage = Storage(db_path)
    await storage.open()
    await storage.insert_observation(
        Observation(
            metric="market.mid_price",
            source="binance",
            scope="USD1USDT",
            value=0.996,
            unit="USDT",
            observed_at=observed_at,
            collected_at=observed_at,
        )
    )
    await storage.set_risk_state(
        "market.price", RiskLevel.YELLOW, observed_at, observed_at
    )
    await storage.close()

    reopened = Storage(db_path)
    await reopened.open()
    state = await reopened.get_risk_state("market.price")
    assert state is not None
    assert state.level is RiskLevel.YELLOW
    assert len(await reopened.latest_observations("market.mid_price", limit=5)) == 1
    await reopened.close()


@pytest.mark.asyncio
async def test_chain_event_history_can_be_limited_to_mutable_state_events(
    storage,
) -> None:
    now = datetime(2026, 9, 8, tzinfo=UTC)
    events = [
        ChainEvent(
            "ethereum",
            100 + index,
            f"0xtx{index}",
            index,
            event_type,
            {"account": "0x" + "11" * 20},
            now,
        )
        for index, event_type in enumerate(
            (
                "TRANSFER",
                "MINT",
                "FREEZE",
                "UNFREEZE",
                "PAUSED",
                "PRIVILEGED_UNPAUSE",
            )
        )
    ]
    await storage.insert_chain_events_and_cursor("ethereum", events, 105)

    mutable = await storage.chain_events_for_chain(
        "ethereum",
        event_types=(
            "FREEZE",
            "UNFREEZE",
            "PAUSED",
            "UNPAUSED",
            "PRIVILEGED_PAUSE",
            "PRIVILEGED_UNPAUSE",
        ),
    )

    assert [event.event_type for event in mutable] == [
        "PRIVILEGED_UNPAUSE",
        "PAUSED",
        "UNFREEZE",
        "FREEZE",
    ]


@pytest.mark.asyncio
async def test_prune_observations_keeps_risk_history(tmp_path: Path) -> None:
    db_path = tmp_path / "monitor.db"
    storage = Storage(db_path)
    await storage.open()
    old_time = datetime(2026, 1, 1, tzinfo=UTC)
    await storage.insert_observation(
        Observation(
            "market.mid_price",
            "binance",
            "USD1USDT",
            1.0,
            "USDT",
            old_time,
            old_time,
        )
    )
    await storage.set_risk_state(
        "market.price", RiskLevel.GREEN, old_time, old_time
    )
    await storage.set_risk_state(
        "event.evm.ethereum.0xold:0", RiskLevel.YELLOW, old_time, old_time
    )

    assert await storage.prune_observations(datetime(2026, 7, 1, tzinfo=UTC)) == 1
    assert await storage.get_risk_state("market.price") is not None
    assert await storage.prune_transient_risk_states(
        datetime(2026, 7, 1, tzinfo=UTC)
    ) == 1
    assert await storage.get_risk_state("market.price") is not None
    assert await storage.get_risk_state("event.evm.ethereum.0xold:0") is None
    await storage.close()


@pytest.mark.asyncio
async def test_prune_keeps_irreversible_evm_event_state(storage) -> None:
    old_time = datetime(2025, 1, 1, tzinfo=UTC)
    rule_id = "evm.event.ethereum.snapshot:100:implementation"
    await storage.set_risk_state(rule_id, RiskLevel.RED, old_time, old_time)

    assert await storage.prune_transient_risk_states(
        datetime(2026, 1, 1, tzinfo=UTC)
    ) == 0
    assert await storage.get_risk_state(rule_id) is not None


@pytest.mark.asyncio
async def test_open_creates_all_monitor_tables(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "monitor.db")
    await storage.open()
    assert await storage.table_names() == {
        "alert_deliveries",
        "announcements",
        "chain_events",
        "chain_block_hashes",
        "collector_health",
        "delivery_rate_limit",
        "observations",
        "risk_states",
        "scan_cursors",
    }
    await storage.close()


@pytest.mark.asyncio
async def test_pending_alert_read_waits_for_writer_commit(storage) -> None:
    inserted = asyncio.Event()
    release = asyncio.Event()

    async def write_then_rollback() -> None:
        async with storage.write_lock:
            await storage.connection.execute("BEGIN IMMEDIATE")
            await storage.insert_pending_alert_uncommitted(
                "uncommitted", "hash", "must not send", datetime.now(UTC)
            )
            inserted.set()
            await release.wait()
            await storage.connection.rollback()

    writer = asyncio.create_task(write_then_rollback())
    await inserted.wait()
    pending_read = asyncio.create_task(storage.pending_alerts())
    await asyncio.sleep(0)

    assert pending_read.done() is False

    release.set()
    await writer
    assert await pending_read == []


@pytest.mark.asyncio
async def test_open_migrates_legacy_delivery_statuses(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE alert_deliveries (
            id INTEGER PRIMARY KEY,
            alert_key TEXT NOT NULL UNIQUE,
            payload_hash TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            delivered_at TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT
        );
        INSERT INTO alert_deliveries VALUES
            (1, 'sent', 'a', 'sent', '2026-01-01T00:00:00+00:00',
             '2026-01-01T00:01:00+00:00', 1, NULL),
            (2, 'failed', 'b', 'failed', '2026-01-01T00:00:00+00:00',
             NULL, 2, 'offline'),
            (3, 'pending', 'c', 'pending', '2026-01-01T00:00:00+00:00',
             NULL, 1, 'offline');
        """
    )
    connection.commit()
    connection.close()

    storage = Storage(db_path)
    await storage.open()
    cursor = await storage.connection.execute(
        "SELECT alert_key, status FROM alert_deliveries ORDER BY id"
    )
    statuses = {row["alert_key"]: row["status"] for row in await cursor.fetchall()}
    await cursor.close()

    assert statuses == {
        "sent": "SENT",
        "failed": "FAILED",
        "pending": "PENDING",
    }
    assert [item.alert_key for item in await storage.pending_alerts()] == ["pending"]
    assert [item.alert_key for item in await storage.failed_alerts()] == ["failed"]
    await storage.close()


@pytest.mark.asyncio
async def test_alert_delivery_can_only_be_claimed_once(storage) -> None:
    await StateEngine(storage).apply(
        [RuleEvaluation("market.price", RiskLevel.YELLOW)],
        datetime.now(UTC),
    )
    pending = (await storage.pending_alerts())[0]

    first, second = await asyncio.gather(
        storage.claim_alert_delivery(pending.id),
        storage.claim_alert_delivery(pending.id),
    )

    assert sorted((first, second)) == [False, True]


@pytest.mark.asyncio
async def test_reopen_recovers_in_flight_alert_to_pending(tmp_path) -> None:
    db_path = tmp_path / "monitor.db"
    storage = Storage(db_path)
    await storage.open()
    await StateEngine(storage).apply(
        [RuleEvaluation("market.price", RiskLevel.YELLOW)],
        datetime.now(UTC),
    )
    pending = (await storage.pending_alerts())[0]
    assert await storage.claim_alert_delivery(pending.id) is True
    await storage.close()

    reopened = Storage(db_path)
    await reopened.open()
    recovered = await reopened.pending_alerts()

    assert [item.id for item in recovered] == [pending.id]
    await reopened.close()


@pytest.mark.asyncio
async def test_cancelled_alert_is_not_revived_by_late_delivery_result(storage) -> None:
    now = datetime.now(UTC)
    await StateEngine(storage).apply(
        [RuleEvaluation("market.price", RiskLevel.YELLOW, cause_id="reorged")],
        now,
    )
    pending = (await storage.pending_alerts())[0]
    assert await storage.claim_alert_delivery(pending.id) is True
    async with storage.write_lock:
        await storage.connection.execute("BEGIN IMMEDIATE")
        await storage.cancel_pending_alerts_for_cause_uncommitted("reorged")
        await storage.connection.commit()

    await storage.record_delivery_result(pending.id, now, error="late failure")

    cursor = await storage.connection.execute(
        "SELECT status, attempts FROM alert_deliveries WHERE id = ?",
        (pending.id,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    assert (row["status"], row["attempts"]) == ("CANCELLED", 0)


@pytest.mark.asyncio
async def test_reorg_cancels_failed_alert_for_same_cause(storage) -> None:
    now = datetime.now(UTC)
    await StateEngine(storage).apply(
        [RuleEvaluation("market.price", RiskLevel.YELLOW, cause_id="orphan")],
        now,
    )
    pending = (await storage.pending_alerts())[0]
    for error in ("first", "second"):
        assert await storage.claim_alert_delivery(pending.id) is True
        await storage.record_delivery_result(pending.id, now, error=error)

    async with storage.write_lock:
        await storage.connection.execute("BEGIN IMMEDIATE")
        await storage.cancel_pending_alerts_for_cause_uncommitted("orphan")
        await storage.connection.commit()

    assert await storage.failed_alerts() == []
    cursor = await storage.connection.execute(
        "SELECT status FROM alert_deliveries WHERE id = ?", (pending.id,)
    )
    row = await cursor.fetchone()
    await cursor.close()
    assert row["status"] == "CANCELLED"


@pytest.mark.asyncio
async def test_reorg_cancels_in_flight_snapshot_before_late_failure(storage) -> None:
    now = datetime.now(UTC)
    async with storage.write_lock:
        await storage.connection.execute("BEGIN IMMEDIATE")
        await storage.insert_pending_alert_uncommitted(
            "snapshot:bsc:100:paused:2026-09-08T00:00:00+00:00",
            "hash",
            "evm.bsc.paused chain=bsc",
            now,
        )
        await storage.connection.commit()
    pending = (await storage.pending_alerts())[0]
    assert await storage.claim_alert_delivery(pending.id) is True

    async with storage.write_lock:
        await storage.connection.execute("BEGIN IMMEDIATE")
        await storage.cancel_reorged_snapshot_alerts_uncommitted("bsc", 100)
        await storage.connection.commit()
    await storage.record_delivery_result(pending.id, now, error="late failure")

    cursor = await storage.connection.execute(
        "SELECT status, attempts FROM alert_deliveries WHERE id = ?",
        (pending.id,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    assert (row["status"], row["attempts"]) == ("CANCELLED", 0)
    assert await storage.pending_alerts() == []


@pytest.mark.asyncio
async def test_reorg_cancels_legacy_snapshot_alert_for_matching_chain(storage) -> None:
    now = datetime.now(UTC)
    async with storage.write_lock:
        await storage.connection.execute("BEGIN IMMEDIATE")
        await storage.insert_pending_alert_uncommitted(
            "snapshot:100:paused:2026-09-08T00:00:00+00:00",
            "ethereum-hash",
            "USD1 risk\n- evm.ethereum.paused: current=True",
            now,
        )
        await storage.insert_pending_alert_uncommitted(
            "snapshot:100:paused:2026-09-08T00:01:00+00:00",
            "bsc-hash",
            "USD1 risk\n- evm.bsc.paused: current=True",
            now,
        )
        await storage.connection.commit()
    ethereum_alert, _ = await storage.pending_alerts()
    for error in ("first", "second"):
        assert await storage.claim_alert_delivery(ethereum_alert.id) is True
        await storage.record_delivery_result(ethereum_alert.id, now, error=error)

    async with storage.write_lock:
        await storage.connection.execute("BEGIN IMMEDIATE")
        await storage.cancel_reorged_snapshot_alerts_uncommitted("ethereum", 100)
        await storage.connection.commit()

    cursor = await storage.connection.execute(
        "SELECT content, status FROM alert_deliveries ORDER BY id"
    )
    statuses = {
        row["content"]: row["status"] for row in await cursor.fetchall()
    }
    await cursor.close()
    assert statuses["USD1 risk\n- evm.ethereum.paused: current=True"] == "CANCELLED"
    assert statuses["USD1 risk\n- evm.bsc.paused: current=True"] == "PENDING"


@pytest.mark.asyncio
async def test_reorg_legacy_immutable_snapshot_alert_is_chain_scoped(storage) -> None:
    now = datetime.now(UTC)
    ethereum_rule = "evm.event.ethereum.snapshot:100:evm.implementation"
    bsc_rule = "evm.event.bsc.snapshot:100:evm.implementation"
    await storage.set_risk_state(ethereum_rule, RiskLevel.RED, now, now)
    await storage.set_risk_state(bsc_rule, RiskLevel.RED, now, now)
    async with storage.write_lock:
        await storage.connection.execute("BEGIN IMMEDIATE")
        await storage.insert_pending_alert_uncommitted(
            "snapshot:100:evm.implementation:2026-09-08T00:00:00+00:00",
            "ethereum-hash",
            f"USD1 risk\n- {ethereum_rule}: implementation changed",
            now,
        )
        await storage.insert_pending_alert_uncommitted(
            "snapshot:100:evm.implementation:2026-09-08T00:01:00+00:00",
            "bsc-hash",
            f"USD1 risk\n- {bsc_rule}: implementation changed",
            now,
        )
        await storage.connection.commit()

    async with storage.write_lock:
        await storage.connection.execute("BEGIN IMMEDIATE")
        await storage.rollback_evm_chain_from_uncommitted("ethereum", 100)
        await storage.connection.commit()

    cursor = await storage.connection.execute(
        "SELECT content, status FROM alert_deliveries ORDER BY id"
    )
    statuses = {
        row["content"]: row["status"] for row in await cursor.fetchall()
    }
    await cursor.close()
    assert statuses[
        f"USD1 risk\n- {ethereum_rule}: implementation changed"
    ] == "CANCELLED"
    assert statuses[
        f"USD1 risk\n- {bsc_rule}: implementation changed"
    ] == "PENDING"
    assert await storage.get_risk_state(ethereum_rule) is None
    assert await storage.get_risk_state(bsc_rule) is not None


@pytest.mark.asyncio
async def test_reorg_cancels_reorg_snapshot_alert_for_matching_chain(
    storage,
) -> None:
    now = datetime.now(UTC)
    async with storage.write_lock:
        await storage.connection.execute("BEGIN IMMEDIATE")
        await storage.insert_pending_alert_uncommitted(
            "reorg-snapshot:ethereum:100:paused:2026-09-08T00:00:00+00:00",
            "ethereum-hash",
            "USD1 risk\n- evm.ethereum.paused: current=False",
            now,
        )
        await storage.insert_pending_alert_uncommitted(
            "reorg-snapshot:bsc:100:paused:2026-09-08T00:01:00+00:00",
            "bsc-hash",
            "USD1 risk\n- evm.bsc.paused: current=False",
            now,
        )
        await storage.connection.commit()

    async with storage.write_lock:
        await storage.connection.execute("BEGIN IMMEDIATE")
        await storage.cancel_reorged_snapshot_alerts_uncommitted("ethereum", 100)
        await storage.connection.commit()

    remaining = {item.alert_key for item in await storage.pending_alerts()}
    assert remaining == {
        "reorg-snapshot:bsc:100:paused:2026-09-08T00:01:00+00:00"
    }


@pytest.mark.asyncio
async def test_reorg_cancels_legacy_reorg_snapshot_alert_by_content_chain(
    storage,
) -> None:
    now = datetime.now(UTC)
    async with storage.write_lock:
        await storage.connection.execute("BEGIN IMMEDIATE")
        await storage.insert_pending_alert_uncommitted(
            "reorg-snapshot:100:paused:2026-09-08T00:00:00+00:00",
            "ethereum-hash",
            "USD1 risk\n- evm.ethereum.paused: current=False",
            now,
        )
        await storage.insert_pending_alert_uncommitted(
            "reorg-snapshot:100:paused:2026-09-08T00:01:00+00:00",
            "bsc-hash",
            "USD1 risk\n- evm.bsc.paused: current=False",
            now,
        )
        await storage.connection.commit()

    async with storage.write_lock:
        await storage.connection.execute("BEGIN IMMEDIATE")
        await storage.cancel_reorged_snapshot_alerts_uncommitted("ethereum", 100)
        await storage.connection.commit()

    remaining = {item.alert_key for item in await storage.pending_alerts()}
    assert remaining == {
        "reorg-snapshot:100:paused:2026-09-08T00:01:00+00:00"
    }
