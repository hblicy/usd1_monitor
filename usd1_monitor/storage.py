from __future__ import annotations

import asyncio
import functools
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite

from usd1_monitor.engine.health import CollectorHealth
from usd1_monitor.models import (
    Announcement,
    ChainEvent,
    Observation,
    PendingAlert,
    RiskLevel,
    RiskState,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY,
    metric TEXT NOT NULL,
    source TEXT NOT NULL,
    scope TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    quality TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_observations_metric_time
ON observations(metric, observed_at DESC);

CREATE TABLE IF NOT EXISTS risk_states (
    rule_id TEXT PRIMARY KEY,
    level INTEGER NOT NULL,
    first_triggered_at TEXT NOT NULL,
    changed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_deliveries (
    id INTEGER PRIMARY KEY,
    alert_key TEXT NOT NULL UNIQUE,
    payload_hash TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
    ,status TEXT NOT NULL DEFAULT 'PENDING'
);

CREATE TABLE IF NOT EXISTS delivery_rate_limit (
    channel TEXT PRIMARY KEY,
    attempts_json TEXT NOT NULL DEFAULT '[]',
    blocked_until TEXT
);

CREATE TABLE IF NOT EXISTS collector_health (
    collector_id TEXT PRIMARY KEY,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_success_at TEXT,
    last_failure_at TEXT,
    last_error TEXT
    ,first_failure_at TEXT
);

CREATE TABLE IF NOT EXISTS chain_events (
    id INTEGER PRIMARY KEY,
    chain TEXT NOT NULL,
    block_number INTEGER NOT NULL,
    tx_hash TEXT NOT NULL,
    log_index INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    UNIQUE(chain, tx_hash, log_index)
);

CREATE TABLE IF NOT EXISTS announcements (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    stable_id TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    published_at TEXT,
    body_hash TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(source, stable_id)
);

CREATE TABLE IF NOT EXISTS scan_cursors (
    chain TEXT PRIMARY KEY,
    safe_block INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chain_block_hashes (
    chain TEXT NOT NULL,
    block_number INTEGER NOT NULL,
    block_hash TEXT NOT NULL,
    PRIMARY KEY(chain, block_number)
);
"""


def serialized_write(method):
    @functools.wraps(method)
    async def wrapper(self, *args, **kwargs):
        async with self.write_lock:
            return await method(self, *args, **kwargs)

    return wrapper


class Storage:
    def __init__(
        self,
        path: Path,
        *,
        timezone_name: str = "Asia/Shanghai",
        event_active_seconds: int = 3600,
    ) -> None:
        self.path = path
        self.timezone_name = timezone_name
        self.event_active_seconds = event_active_seconds
        self._connection: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    @property
    def write_lock(self) -> asyncio.Lock:
        return self._write_lock

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("storage is not open")
        return self._connection

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(self.path)
        self._connection.row_factory = aiosqlite.Row
        await self._connection.execute("PRAGMA journal_mode=WAL")
        await self._connection.execute("PRAGMA foreign_keys=ON")
        await self._connection.executescript(SCHEMA)
        await self._ensure_column(
            "announcements", "metadata_json", "TEXT NOT NULL DEFAULT '{}'"
        )
        await self._ensure_column(
            "alert_deliveries", "status", "TEXT NOT NULL DEFAULT 'PENDING'"
        )
        await self._connection.execute(
            "UPDATE alert_deliveries SET status = 'PENDING' WHERE status = 'IN_FLIGHT'"
        )
        await self._connection.execute(
            """
            UPDATE alert_deliveries
            SET status = CASE
                WHEN delivered_at IS NOT NULL THEN 'SENT'
                WHEN attempts >= 2 THEN 'FAILED'
                ELSE 'PENDING'
            END
            WHERE status = 'PENDING'
              AND (delivered_at IS NOT NULL OR attempts >= 2)
            """
        )
        await self._ensure_column(
            "collector_health", "first_failure_at", "TEXT"
        )
        await self._connection.commit()

    async def _ensure_column(
        self, table: str, column: str, definition: str
    ) -> None:
        cursor = await self.connection.execute(f"PRAGMA table_info({table})")
        columns = {row["name"] for row in await cursor.fetchall()}
        await cursor.close()
        if column not in columns:
            await self.connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
            )

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    @serialized_write
    async def insert_observation(self, observation: Observation) -> None:
        await self.insert_observation_uncommitted(observation)
        await self.connection.commit()

    async def insert_observation_uncommitted(
        self, observation: Observation
    ) -> None:
        await self.connection.execute(
            """
            INSERT INTO observations (
                metric, source, scope, value, unit, observed_at,
                collected_at, quality, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observation.metric,
                observation.source,
                observation.scope,
                observation.value,
                observation.unit,
                observation.observed_at.isoformat(),
                observation.collected_at.isoformat(),
                observation.quality,
                json.dumps(observation.metadata, sort_keys=True),
            ),
        )

    async def delete_latest_observation_uncommitted(
        self, metric: str, scope: str
    ) -> None:
        await self.connection.execute(
            """
            DELETE FROM observations
            WHERE id = (
                SELECT id
                FROM observations
                WHERE metric = ? AND scope = ?
                ORDER BY observed_at DESC, id DESC
                LIMIT 1
            )
            """,
            (metric, scope),
        )

    async def latest_observations(
        self, metric: str, *, limit: int
    ) -> list[Observation]:
        cursor = await self.connection.execute(
            """
            SELECT metric, source, scope, value, unit, observed_at,
                   collected_at, quality, metadata_json
            FROM observations
            WHERE metric = ?
            ORDER BY observed_at DESC
            LIMIT ?
            """,
            (metric, limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            Observation(
                metric=row["metric"],
                source=row["source"],
                scope=row["scope"],
                value=row["value"],
                unit=row["unit"],
                observed_at=datetime.fromisoformat(row["observed_at"]),
                collected_at=datetime.fromisoformat(row["collected_at"]),
                quality=row["quality"],
                metadata=json.loads(row["metadata_json"]),
            )
            for row in rows
        ]

    async def observations_since(
        self, metrics: tuple[str, ...], since: datetime
    ) -> list[Observation]:
        if not metrics:
            return []
        placeholders = ",".join("?" for _ in metrics)
        cursor = await self.connection.execute(
            f"""
            SELECT metric, source, scope, value, unit, observed_at,
                   collected_at, quality, metadata_json
            FROM observations
            WHERE metric IN ({placeholders}) AND observed_at >= ?
            ORDER BY observed_at, id
            """,
            (*metrics, since.isoformat()),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            Observation(
                metric=row["metric"],
                source=row["source"],
                scope=row["scope"],
                value=row["value"],
                unit=row["unit"],
                observed_at=datetime.fromisoformat(row["observed_at"]),
                collected_at=datetime.fromisoformat(row["collected_at"]),
                quality=row["quality"],
                metadata=json.loads(row["metadata_json"]),
            )
            for row in rows
        ]

    @serialized_write
    async def set_risk_state(
        self,
        rule_id: str,
        level: RiskLevel,
        first_triggered_at: datetime,
        changed_at: datetime,
    ) -> None:
        await self.upsert_risk_state_uncommitted(
            rule_id, level, first_triggered_at, changed_at
        )
        await self.connection.commit()

    async def upsert_risk_state_uncommitted(
        self,
        rule_id: str,
        level: RiskLevel,
        first_triggered_at: datetime,
        changed_at: datetime,
    ) -> None:
        await self.connection.execute(
            """
            INSERT INTO risk_states (
                rule_id, level, first_triggered_at, changed_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(rule_id) DO UPDATE SET
                level = excluded.level,
                first_triggered_at = excluded.first_triggered_at,
                changed_at = excluded.changed_at
            """,
            (
                rule_id,
                int(level),
                first_triggered_at.isoformat(),
                changed_at.isoformat(),
            ),
        )

    async def insert_pending_alert_uncommitted(
        self,
        alert_key: str,
        payload_hash: str,
        content: str,
        created_at: datetime,
    ) -> None:
        await self.connection.execute(
            """
            INSERT INTO alert_deliveries (
                alert_key, payload_hash, content, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (alert_key, payload_hash, content, created_at.isoformat()),
        )

    async def count_alert_deliveries(self) -> int:
        cursor = await self.connection.execute(
            "SELECT COUNT(*) AS count FROM alert_deliveries"
        )
        row = await cursor.fetchone()
        await cursor.close()
        return int(row["count"])

    @serialized_write
    async def pending_alerts(self, *, max_attempts: int = 2) -> list[PendingAlert]:
        cursor = await self.connection.execute(
            """
            SELECT id, alert_key, content, attempts
            FROM alert_deliveries
            WHERE delivered_at IS NULL AND attempts < ?
              AND status = 'PENDING'
            ORDER BY created_at, id
            """,
            (max_attempts,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            PendingAlert(
                id=row["id"],
                alert_key=row["alert_key"],
                content=row["content"],
                attempts=row["attempts"],
            )
            for row in rows
        ]

    async def failed_alerts(self) -> list[PendingAlert]:
        cursor = await self.connection.execute(
            """
            SELECT id, alert_key, content, attempts
            FROM alert_deliveries
            WHERE status = 'FAILED'
            ORDER BY created_at, id
            """
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            PendingAlert(row["id"], row["alert_key"], row["content"], row["attempts"])
            for row in rows
        ]

    @serialized_write
    async def try_acquire_delivery_slot(
        self,
        channel: str,
        attempted_at: datetime,
        *,
        max_messages: int,
        window_seconds: float,
    ) -> bool:
        await self.connection.execute("BEGIN IMMEDIATE")
        try:
            acquired = await self._try_acquire_delivery_slot_uncommitted(
                channel,
                attempted_at,
                max_messages=max_messages,
                window_seconds=window_seconds,
            )
            await self.connection.commit()
            return acquired
        except BaseException:
            await self.connection.rollback()
            raise

    async def _try_acquire_delivery_slot_uncommitted(
        self,
        channel: str,
        attempted_at: datetime,
        *,
        max_messages: int,
        window_seconds: float,
    ) -> bool:
        cutoff = attempted_at - timedelta(seconds=window_seconds)
        cursor = await self.connection.execute(
            """
            SELECT attempts_json, blocked_until
            FROM delivery_rate_limit
            WHERE channel = ?
            """,
            (channel,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        attempts = (
            [
                datetime.fromisoformat(value)
                for value in json.loads(row["attempts_json"])
            ]
            if row is not None
            else []
        )
        attempts = [value for value in attempts if value > cutoff]
        blocked_until = (
            datetime.fromisoformat(row["blocked_until"])
            if row is not None and row["blocked_until"] is not None
            else None
        )
        acquired = (
            (blocked_until is None or attempted_at >= blocked_until)
            and len(attempts) < max_messages
        )
        if acquired:
            attempts.append(attempted_at)
        await self.connection.execute(
            """
            INSERT INTO delivery_rate_limit (
                channel, attempts_json, blocked_until
            ) VALUES (?, ?, ?)
            ON CONFLICT(channel) DO UPDATE SET
                attempts_json = excluded.attempts_json,
                blocked_until = excluded.blocked_until
            """,
            (
                channel,
                json.dumps([value.isoformat() for value in attempts]),
                (
                    blocked_until.isoformat()
                    if blocked_until is not None and blocked_until > attempted_at
                    else None
                ),
            ),
        )
        return acquired

    @serialized_write
    async def claim_alert_delivery_with_slot(
        self,
        alert_id: int,
        channel: str,
        attempted_at: datetime,
        *,
        max_messages: int,
        window_seconds: float,
    ) -> tuple[bool, bool]:
        await self.connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = await self.connection.execute(
                """
                UPDATE alert_deliveries
                SET status = 'IN_FLIGHT'
                WHERE id = ? AND delivered_at IS NULL
                  AND attempts < 2 AND status = 'PENDING'
                """,
                (alert_id,),
            )
            claimed = cursor.rowcount == 1
            await cursor.close()
            if not claimed:
                await self.connection.commit()
                return True, False

            acquired = await self._try_acquire_delivery_slot_uncommitted(
                channel,
                attempted_at,
                max_messages=max_messages,
                window_seconds=window_seconds,
            )
            if not acquired:
                await self.connection.execute(
                    """
                    UPDATE alert_deliveries SET status = 'PENDING'
                    WHERE id = ? AND status = 'IN_FLIGHT'
                    """,
                    (alert_id,),
                )
            await self.connection.commit()
            return acquired, acquired
        except BaseException:
            await self.connection.rollback()
            raise

    @serialized_write
    async def defer_delivery(
        self,
        channel: str,
        blocked_until: datetime,
    ) -> None:
        await self.connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = await self.connection.execute(
                "SELECT blocked_until FROM delivery_rate_limit WHERE channel = ?",
                (channel,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            existing = (
                datetime.fromisoformat(row["blocked_until"])
                if row is not None and row["blocked_until"] is not None
                else None
            )
            effective = (
                max(existing, blocked_until)
                if existing is not None
                else blocked_until
            )
            await self.connection.execute(
                """
                INSERT INTO delivery_rate_limit (
                    channel, attempts_json, blocked_until
                ) VALUES (?, '[]', ?)
                ON CONFLICT(channel) DO UPDATE SET
                    blocked_until = excluded.blocked_until
                """,
                (channel, effective.isoformat()),
            )
            await self.connection.commit()
        except BaseException:
            await self.connection.rollback()
            raise

    @serialized_write
    async def claim_alert_delivery(self, alert_id: int) -> bool:
        cursor = await self.connection.execute(
            """
            UPDATE alert_deliveries
            SET status = 'IN_FLIGHT'
            WHERE id = ? AND delivered_at IS NULL
              AND attempts < 2 AND status = 'PENDING'
            """,
            (alert_id,),
        )
        claimed = cursor.rowcount == 1
        await cursor.close()
        await self.connection.commit()
        return claimed

    @serialized_write
    async def record_delivery_result(
        self,
        alert_id: int,
        attempted_at: datetime,
        *,
        error: str | None,
    ) -> None:
        await self.connection.execute(
            """
            UPDATE alert_deliveries
            SET attempts = attempts + 1,
                delivered_at = CASE WHEN ? IS NULL THEN ? ELSE NULL END,
                last_error = ?,
                status = CASE
                    WHEN ? IS NULL THEN 'SENT'
                    WHEN attempts + 1 >= 2 THEN 'FAILED'
                    ELSE 'PENDING'
                END
            WHERE id = ? AND status = 'IN_FLIGHT'
            """,
            (error, attempted_at.isoformat(), error, error, alert_id),
        )
        await self.connection.commit()

    @serialized_write
    async def record_collector_failure(
        self, collector_id: str, failed_at: datetime, error: str
    ) -> None:
        await self.connection.execute(
            """
            INSERT INTO collector_health (
                collector_id, consecutive_failures, last_failure_at,
                last_error, first_failure_at
            ) VALUES (?, 1, ?, ?, ?)
            ON CONFLICT(collector_id) DO UPDATE SET
                consecutive_failures = collector_health.consecutive_failures + 1,
                last_failure_at = excluded.last_failure_at,
                last_error = excluded.last_error,
                first_failure_at = COALESCE(
                    collector_health.first_failure_at,
                    excluded.first_failure_at
                )
            """,
            (collector_id, failed_at.isoformat(), error, failed_at.isoformat()),
        )
        await self.connection.commit()

    @serialized_write
    async def record_collector_success(
        self, collector_id: str, succeeded_at: datetime
    ) -> None:
        await self.connection.execute(
            """
            INSERT INTO collector_health (
                collector_id, consecutive_failures, last_success_at, last_error
            ) VALUES (?, 0, ?, NULL)
            ON CONFLICT(collector_id) DO UPDATE SET
                consecutive_failures = 0,
                last_success_at = excluded.last_success_at,
                last_error = NULL,
                first_failure_at = NULL
            """,
            (collector_id, succeeded_at.isoformat()),
        )
        await self.connection.commit()

    async def get_collector_health(
        self, collector_id: str
    ) -> CollectorHealth | None:
        cursor = await self.connection.execute(
            """
            SELECT collector_id, consecutive_failures, last_success_at,
                   last_failure_at, last_error, first_failure_at
            FROM collector_health
            WHERE collector_id = ?
            """,
            (collector_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return CollectorHealth(
            collector_id=row["collector_id"],
            consecutive_failures=row["consecutive_failures"],
            last_success_at=(
                datetime.fromisoformat(row["last_success_at"])
                if row["last_success_at"]
                else None
            ),
            last_error=row["last_error"],
            last_failure_at=(
                datetime.fromisoformat(row["last_failure_at"])
                if row["last_failure_at"]
                else None
            ),
            first_failure_at=(
                datetime.fromisoformat(row["first_failure_at"])
                if row["first_failure_at"]
                else None
            ),
        )

    async def list_collector_health(self) -> list[CollectorHealth]:
        cursor = await self.connection.execute(
            "SELECT collector_id FROM collector_health ORDER BY collector_id"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        result: list[CollectorHealth] = []
        for row in rows:
            item = await self.get_collector_health(row["collector_id"])
            if item is not None:
                result.append(item)
        return result

    async def get_risk_state(self, rule_id: str) -> RiskState | None:
        cursor = await self.connection.execute(
            """
            SELECT rule_id, level, first_triggered_at, changed_at
            FROM risk_states
            WHERE rule_id = ?
            """,
            (rule_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return RiskState(
            rule_id=row["rule_id"],
            level=RiskLevel(row["level"]),
            first_triggered_at=datetime.fromisoformat(row["first_triggered_at"]),
            changed_at=datetime.fromisoformat(row["changed_at"]),
        )

    async def list_risk_states(self) -> list[RiskState]:
        cursor = await self.connection.execute(
            """
            SELECT rule_id, level, first_triggered_at, changed_at
            FROM risk_states
            ORDER BY rule_id
            """
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            RiskState(
                rule_id=row["rule_id"],
                level=RiskLevel(row["level"]),
                first_triggered_at=datetime.fromisoformat(
                    row["first_triggered_at"]
                ),
                changed_at=datetime.fromisoformat(row["changed_at"]),
            )
            for row in rows
        ]

    async def expired_event_risk_states(
        self, cutoff: datetime
    ) -> list[RiskState]:
        cursor = await self.connection.execute(
            """
            SELECT rule_id, level, first_triggered_at, changed_at
            FROM risk_states
            WHERE level != ? AND changed_at <= ?
              AND rule_id LIKE 'event.%'
            ORDER BY changed_at, rule_id
            """,
            (int(RiskLevel.GREEN), cutoff.isoformat()),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            RiskState(
                rule_id=row["rule_id"],
                level=RiskLevel(row["level"]),
                first_triggered_at=datetime.fromisoformat(
                    row["first_triggered_at"]
                ),
                changed_at=datetime.fromisoformat(row["changed_at"]),
            )
            for row in rows
        ]

    @serialized_write
    async def prune_observations(self, cutoff: datetime) -> int:
        cursor = await self.connection.execute(
            "DELETE FROM observations WHERE observed_at < ?",
            (cutoff.isoformat(),),
        )
        await self.connection.commit()
        count = cursor.rowcount
        await cursor.close()
        return count

    @serialized_write
    async def prune_transient_risk_states(self, cutoff: datetime) -> int:
        cursor = await self.connection.execute(
            """
            DELETE FROM risk_states
            WHERE changed_at < ?
              AND rule_id LIKE 'event.%'
            """,
            (cutoff.isoformat(),),
        )
        await self.connection.commit()
        count = cursor.rowcount
        await cursor.close()
        return count

    @serialized_write
    async def set_scan_cursor(self, chain: str, safe_block: int) -> None:
        await self.set_scan_cursor_uncommitted(chain, safe_block)
        await self.connection.commit()

    async def set_scan_cursor_uncommitted(
        self, chain: str, safe_block: int
    ) -> None:
        await self.connection.execute(
            """
            INSERT INTO scan_cursors (chain, safe_block, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(chain) DO UPDATE SET
                safe_block = excluded.safe_block,
                updated_at = excluded.updated_at
            """,
            (chain, safe_block, datetime.now(UTC).isoformat()),
        )

    async def get_scan_cursor(self, chain: str) -> int | None:
        cursor = await self.connection.execute(
            "SELECT safe_block FROM scan_cursors WHERE chain = ?", (chain,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return None if row is None else int(row["safe_block"])

    @serialized_write
    async def insert_chain_events_and_cursor(
        self, chain: str, events: list[ChainEvent], safe_block: int
    ) -> list[ChainEvent]:
        await self.connection.execute("BEGIN IMMEDIATE")
        try:
            inserted = await self.insert_chain_events_uncommitted(events)
            await self.set_scan_cursor_uncommitted(chain, safe_block)
            await self.connection.commit()
        except BaseException:
            await self.connection.rollback()
            raise
        return inserted

    async def insert_chain_events_uncommitted(
        self, events: list[ChainEvent]
    ) -> list[ChainEvent]:
        inserted: list[ChainEvent] = []
        for event in events:
            cursor = await self.connection.execute(
                """
                INSERT OR IGNORE INTO chain_events (
                    chain, block_number, tx_hash, log_index, event_type,
                    payload_json, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.chain,
                    event.block_number,
                    event.tx_hash,
                    event.log_index,
                    event.event_type,
                    json.dumps(event.payload, sort_keys=True),
                    event.observed_at.isoformat(),
                ),
            )
            if cursor.rowcount > 0:
                inserted.append(event)
            await cursor.close()
        return inserted

    async def reconcile_chain_events_uncommitted(
        self,
        chain: str,
        start_block: int,
        end_block: int,
        canonical_events: list[ChainEvent],
        *,
        include_privileged: bool,
    ) -> list[ChainEvent]:
        query = """
            SELECT id, chain, block_number, tx_hash, log_index, event_type,
                   payload_json, observed_at
            FROM chain_events
            WHERE chain = ? AND block_number BETWEEN ? AND ?
        """
        params: list[object] = [chain, start_block, end_block]
        if not include_privileged:
            query += " AND event_type NOT LIKE 'PRIVILEGED_%'"
        cursor = await self.connection.execute(query, params)
        rows = await cursor.fetchall()
        await cursor.close()
        canonical_keys = {
            (event.tx_hash.lower(), event.log_index) for event in canonical_events
        }
        removed_rows = [
            row
            for row in rows
            if (row["tx_hash"].lower(), row["log_index"]) not in canonical_keys
        ]
        if removed_rows:
            placeholders = ",".join("?" for _ in removed_rows)
            await self.connection.execute(
                f"DELETE FROM chain_events WHERE id IN ({placeholders})",
                [row["id"] for row in removed_rows],
            )
        return [self._chain_event_from_row(row) for row in removed_rows]

    async def block_hash_reorg_from(
        self, chain: str, block_hashes: dict[int, str]
    ) -> int | None:
        if not block_hashes:
            return None
        placeholders = ",".join("?" for _ in block_hashes)
        cursor = await self.connection.execute(
            f"""
            SELECT block_number, block_hash FROM chain_block_hashes
            WHERE chain = ? AND block_number IN ({placeholders})
            """,
            (chain, *block_hashes.keys()),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        changed = [
            int(row["block_number"])
            for row in rows
            if row["block_hash"].lower()
            != block_hashes[int(row["block_number"])].lower()
        ]
        return min(changed) if changed else None

    async def upsert_block_hashes_uncommitted(
        self, chain: str, block_hashes: dict[int, str]
    ) -> None:
        for block_number, block_hash in block_hashes.items():
            await self.connection.execute(
                """
                INSERT INTO chain_block_hashes (chain, block_number, block_hash)
                VALUES (?, ?, ?)
                ON CONFLICT(chain, block_number) DO UPDATE SET
                    block_hash = excluded.block_hash
                """,
                (chain, block_number, block_hash),
            )
        if block_hashes:
            await self.connection.execute(
                """
                DELETE FROM chain_block_hashes
                WHERE chain = ? AND block_number < ?
                """,
                (chain, min(block_hashes)),
            )

    async def rollback_evm_chain_from_uncommitted(
        self, chain: str, block_number: int
    ) -> list[ChainEvent]:
        cursor = await self.connection.execute(
            """
            SELECT id, chain, block_number, tx_hash, log_index, event_type,
                   payload_json, observed_at
            FROM chain_events
            WHERE chain = ? AND block_number >= ?
            """,
            (chain, block_number),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        await self.connection.execute(
            "DELETE FROM chain_events WHERE chain = ? AND block_number >= ?",
            (chain, block_number),
        )
        await self.delete_evm_observations_from_uncommitted(chain, block_number)
        await self.connection.execute(
            "DELETE FROM chain_block_hashes WHERE chain = ? AND block_number >= ?",
            (chain, block_number),
        )
        await self.cancel_reorged_snapshot_alerts_uncommitted(chain, block_number)

        prefix = f"evm.event.{chain}.snapshot:"
        cursor = await self.connection.execute(
            "SELECT rule_id FROM risk_states WHERE rule_id LIKE ?",
            (f"{prefix}%",),
        )
        state_rows = await cursor.fetchall()
        await cursor.close()
        for row in state_rows:
            rule_id = str(row["rule_id"])
            remainder = rule_id[len(prefix):]
            raw_block = remainder.split(":", 1)[0]
            try:
                state_block = int(raw_block)
            except ValueError:
                continue
            if state_block >= block_number:
                await self.delete_risk_state_uncommitted(rule_id)
        return [self._chain_event_from_row(row) for row in rows]

    async def cancel_reorged_snapshot_alerts_uncommitted(
        self, chain: str, from_block: int
    ) -> None:
        cursor = await self.connection.execute(
            """
            SELECT id, alert_key, content
            FROM alert_deliveries
            WHERE delivered_at IS NULL
              AND status IN ('PENDING', 'FAILED', 'IN_FLIGHT')
              AND (
                  alert_key LIKE 'snapshot:%'
                  OR alert_key LIKE 'reorg-snapshot:%'
              )
            """
        )
        rows = await cursor.fetchall()
        await cursor.close()
        cancelled_ids: list[tuple[int]] = []
        for row in rows:
            parts = str(row["alert_key"]).split(":")
            if len(parts) < 4 or parts[0] not in {
                "snapshot", "reorg-snapshot"
            }:
                continue
            if parts[1] == chain:
                block_index = 2
            elif any(
                marker in str(row["content"])
                for marker in (
                    f"chain={chain}",
                    f"evm.{chain}.paused",
                    f"evm.{chain}.freeze.",
                    f"evm.event.{chain}.snapshot:",
                )
            ):
                block_index = 1
            else:
                continue
            try:
                snapshot_block = int(parts[block_index])
            except (IndexError, ValueError):
                continue
            fact_index = block_index + 1
            if (
                snapshot_block >= from_block
                and len(parts) > fact_index
                and parts[fact_index]
                in {
                    "paused",
                    "frozen",
                    "evm.implementation",
                    "evm.admin",
                    "evm.code_hash",
                    "evm.owner",
                }
            ):
                cancelled_ids.append((int(row["id"]),))
        if cancelled_ids:
            await self.connection.executemany(
                "UPDATE alert_deliveries SET status = 'CANCELLED' WHERE id = ?",
                cancelled_ids,
            )

    async def chain_events_for_chain(
        self, chain: str, *, event_types: tuple[str, ...] | None = None
    ) -> list[ChainEvent]:
        if event_types is not None and not event_types:
            return []
        event_filter = ""
        parameters: list[object] = [chain]
        if event_types is not None:
            placeholders = ", ".join("?" for _ in event_types)
            event_filter = f" AND event_type IN ({placeholders})"
            parameters.extend(event_types)
        cursor = await self.connection.execute(
            f"""
            SELECT chain, block_number, tx_hash, log_index, event_type,
                   payload_json, observed_at
            FROM chain_events
            WHERE chain = ?{event_filter}
            ORDER BY block_number DESC, log_index DESC, id DESC
            """,
            parameters,
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._chain_event_from_row(row) for row in rows]

    async def delete_evm_observations_from_uncommitted(
        self, chain: str, block_number: int
    ) -> None:
        await self.connection.execute(
            """
            DELETE FROM observations
            WHERE source = 'evm_rpc' AND scope = ?
              AND CAST(json_extract(metadata_json, '$.block') AS INTEGER) >= ?
            """,
            (chain, block_number),
        )

    async def delete_risk_state_uncommitted(self, rule_id: str) -> None:
        await self.connection.execute(
            "DELETE FROM risk_states WHERE rule_id = ?", (rule_id,)
        )

    async def cancel_pending_alerts_for_cause_uncommitted(
        self, cause_id: str
    ) -> None:
        await self.connection.execute(
            """
            UPDATE alert_deliveries SET status = 'CANCELLED'
            WHERE delivered_at IS NULL
              AND status IN ('PENDING', 'FAILED', 'IN_FLIGHT')
              AND alert_key LIKE ?
            """,
            (f"{cause_id}:%",),
        )

    async def latest_observation(
        self, metric: str, scope: str
    ) -> Observation | None:
        cursor = await self.connection.execute(
            """
            SELECT metric, source, scope, value, unit, observed_at,
                   collected_at, quality, metadata_json
            FROM observations
            WHERE metric = ? AND scope = ?
            ORDER BY observed_at DESC, id DESC
            LIMIT 1
            """,
            (metric, scope),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return Observation(
            metric=row["metric"],
            source=row["source"],
            scope=row["scope"],
            value=row["value"],
            unit=row["unit"],
            observed_at=datetime.fromisoformat(row["observed_at"]),
            collected_at=datetime.fromisoformat(row["collected_at"]),
            quality=row["quality"],
            metadata=json.loads(row["metadata_json"]),
        )

    async def count_chain_events(self) -> int:
        cursor = await self.connection.execute(
            "SELECT COUNT(*) AS count FROM chain_events"
        )
        row = await cursor.fetchone()
        await cursor.close()
        return int(row["count"])

    async def recent_chain_events(self, *, limit: int = 20) -> list[ChainEvent]:
        cursor = await self.connection.execute(
            """
            SELECT chain, block_number, tx_hash, log_index, event_type,
                   payload_json, observed_at
            FROM chain_events
            ORDER BY block_number DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._chain_event_from_row(row) for row in rows]

    @staticmethod
    def _chain_event_from_row(row: aiosqlite.Row) -> ChainEvent:
        return ChainEvent(
            chain=row["chain"],
            block_number=row["block_number"],
            tx_hash=row["tx_hash"],
            log_index=row["log_index"],
            event_type=row["event_type"],
            payload=json.loads(row["payload_json"]),
            observed_at=datetime.fromisoformat(row["observed_at"]),
        )

    @serialized_write
    async def upsert_announcement(self, item: Announcement) -> str:
        outcome = await self.upsert_announcement_uncommitted(item)
        await self.connection.commit()
        return outcome

    async def upsert_announcement_uncommitted(self, item: Announcement) -> str:
        cursor = await self.connection.execute(
            """
            SELECT body_hash, metadata_json FROM announcements
            WHERE source = ? AND stable_id = ?
            """,
            (item.source, item.stable_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            outcome = "NEW"
        elif row["body_hash"] == item.body_hash:
            outcome = "UNCHANGED"
        else:
            previous_metadata = json.loads(row["metadata_json"])
            outcome = (
                "BASELINED"
                if item.metadata.get("content_version")
                and not previous_metadata.get("content_version")
                else "CHANGED"
            )
        await self.connection.execute(
            """
            INSERT INTO announcements (
                source, stable_id, title, url, published_at, body_hash,
                metadata_json, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, stable_id) DO UPDATE SET
                title = excluded.title,
                url = excluded.url,
                published_at = excluded.published_at,
                body_hash = excluded.body_hash,
                metadata_json = excluded.metadata_json,
                last_seen_at = excluded.last_seen_at
            """,
            (
                item.source,
                item.stable_id,
                item.title,
                item.url,
                item.published_at.isoformat() if item.published_at else None,
                item.body_hash,
                json.dumps(item.metadata, sort_keys=True),
                item.first_seen_at.isoformat(),
                item.first_seen_at.isoformat(),
            ),
        )
        return outcome

    async def announcement_stable_ids(self, source: str) -> set[str]:
        cursor = await self.connection.execute(
            "SELECT stable_id FROM announcements WHERE source = ?",
            (source,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return {str(row["stable_id"]) for row in rows}

    async def recent_announcement_stable_ids(
        self, source: str, since: datetime
    ) -> set[str]:
        cursor = await self.connection.execute(
            """
            SELECT stable_id FROM announcements
            WHERE source = ? AND last_seen_at >= ?
            """,
            (source, since.isoformat()),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return {str(row["stable_id"]) for row in rows}

    async def recent_announcement_failure_ids(
        self, source: str, since: datetime
    ) -> set[str]:
        cursor = await self.connection.execute(
            """
            SELECT stable_id, metadata_json FROM announcements
            WHERE source = ? AND last_seen_at >= ?
            """,
            (source, since.isoformat()),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return {
            str(row["stable_id"])
            for row in rows
            if json.loads(row["metadata_json"]).get("scan_error")
        }

    async def latest_announcement(self, source: str) -> Announcement | None:
        cursor = await self.connection.execute(
            """
            SELECT source, stable_id, title, url, published_at, body_hash,
                   first_seen_at, metadata_json
            FROM announcements
            WHERE source = ?
            ORDER BY
                CASE
                    WHEN source = 'bitgo' THEN substr(stable_id, 1, 7)
                    ELSE COALESCE(published_at, first_seen_at)
                END DESC,
                id DESC
            LIMIT 1
            """,
            (source,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return Announcement(
            source=row["source"],
            stable_id=row["stable_id"],
            title=row["title"],
            url=row["url"],
            published_at=(
                datetime.fromisoformat(row["published_at"])
                if row["published_at"]
                else None
            ),
            body_hash=row["body_hash"],
            first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
            metadata=json.loads(row["metadata_json"]),
        )

    async def table_names(self) -> set[str]:
        cursor = await self.connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            """
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return {row["name"] for row in rows}
