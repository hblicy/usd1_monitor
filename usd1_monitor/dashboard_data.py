from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
import aiosqlite

from usd1_monitor.engine.aggregate import (
    business_overall,
    health_overall,
    is_monitoring_health_rule,
)
from usd1_monitor.engine.asset_assessment import (
    POR_COVERAGE_MAX_AGE_SECONDS,
    Pillar,
    assess_asset,
    pillar_from_observations,
)
from usd1_monitor.http import sanitize_url
from usd1_monitor.models import Observation, RiskLevel, RiskState
from usd1_monitor.time_utils import local_iso


REQUIRED_TABLES = {
    "risk_states",
    "observations",
    "collector_health",
    "alert_deliveries",
    "chain_events",
    "announcements",
}

RULE_LABELS = {
    "market.price": "USD1 价格偏离正常范围",
    "por.age": "USD1 储备数据长时间没有更新",
    "por.reserve_change": "USD1 储备数据发生明显变化",
    "supply.collateralization": "USD1 储备与供应量不匹配",
    "supply.multichain": "USD1 多链供应量核对异常",
    "evm.ethereum": "Ethereum 合约状态发生变化",
    "evm.bsc": "BNB Chain 合约状态发生变化",
    "health.evm_ethereum": "Ethereum 链上数据获取异常",
    "health.evm_bsc": "BNB Chain 链上数据获取异常",
    "health.por": "储备数据获取异常",
    "health.supply": "供应量数据获取异常",
    "health.monitor_stale": "监控数据已经停止更新",
}

MONITOR_STALE_SECONDS = 900
LEGACY_COLLECTOR_IDS = frozenset({"supply_ethereum", "supply_bsc"})
LEGACY_HEALTH_RULE_IDS = frozenset(
    f"health.{collector_id}" for collector_id in LEGACY_COLLECTOR_IDS
)
OFFICIAL_ANNOUNCEMENT_SOURCES = (
    "binance",
    "bitgo",
    "wlfi",
    "occ",
    "redemption_page",
)
FLOW_COHORT_CANDIDATE_LIMIT = 512
FLOW_WINDOW_HOURS = {
    "custody.binance_net_change_24h": 24,
    "custody.address_external_outflow_1h": 1,
    "custody.solana_balance_delta_1h": 1,
    "custody.solana_balance_delta_24h": 24,
}

METRIC_KEYS = {
    ("market.mid_price", "USD1USDT"): "price_usd1usdt",
    ("market.mid_price", "USD1USDC"): "price_usd1usdc",
    ("market.sell_1000000_terminal_price", "USD1USDT"): "exit_usd1usdt_1m",
    ("market.sell_1000000_terminal_price", "USD1USDC"): "exit_usd1usdc_1m",
    ("por.reserves", "ethereum"): "reserves",
    ("supply.multichain_total", "global"): "multichain_supply",
    ("supply.estimated_collateralization", "global"): "estimated_collateralization",
    ("supply.bridged_total", "global"): "bridged_total",
    ("bridge.locked_total", "global"): "locked_total",
    ("bridge.issuance_delta", "global"): "bridge_delta",
}

URL_PATTERN = re.compile(r"https?://[^\s<>\"'，。；、]+", re.IGNORECASE)
ALERT_PART_PATTERN = re.compile(r":part:\d{3}$")
LEGACY_RULE_PATTERN = re.compile(
    r"(?:^|\n)-\s+([^\s:]+):\s+当前值=", re.IGNORECASE
)


def _safe_http_url(value: str, *, preserve_path: bool) -> str | None:
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.casefold()
    if scheme not in {"http", "https"} or not parts.hostname:
        return None
    hostname = parts.hostname
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = f"{hostname}:{port}" if port is not None else hostname
    path = parts.path if preserve_path else ""
    return urlunsplit((scheme, netloc, path, "", ""))


def sanitize_dashboard_error(value: str | None) -> str | None:
    if value is None:
        return None

    def replace(match: re.Match[str]) -> str:
        raw = match.group(0).rstrip(")]};,!?")
        try:
            return sanitize_url(raw)
        except ValueError:
            return "[link]"

    return URL_PATTERN.sub(replace, value)[:300]


def sanitize_dashboard_text(value: str | None) -> str | None:
    if value is None:
        return None

    def replace(match: re.Match[str]) -> str:
        raw = match.group(0).rstrip(")]};,!?")
        return _safe_http_url(raw, preserve_path=True) or "[link]"

    return URL_PATTERN.sub(replace, value)


def safe_external_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return _safe_http_url(value, preserve_path=True)


def base_alert_key(value: str) -> str:
    return ALERT_PART_PATTERN.sub("", value)


class DashboardDataError(RuntimeError):
    pass


class DashboardRepository:
    def __init__(
        self,
        path: Path,
        *,
        timezone_name: str = "Asia/Shanghai",
        event_active_seconds: int = 3600,
        coverage_max_age_seconds: int = POR_COVERAGE_MAX_AGE_SECONDS,
    ) -> None:
        if (
            type(coverage_max_age_seconds) is not int
            or coverage_max_age_seconds <= 0
        ):
            raise ValueError(
                "coverage_max_age_seconds must be a positive integer"
            )
        self.path = path
        self.timezone_name = timezone_name
        self.event_active_seconds = event_active_seconds
        self.coverage_max_age_seconds = coverage_max_age_seconds
        self._connection: aiosqlite.Connection | None = None
        self._snapshot_lock = asyncio.Lock()

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("dashboard repository is not open")
        return self._connection

    async def open(self) -> None:
        if not self.path.is_file():
            raise DashboardDataError(f"database does not exist: {self.path}")
        uri = f"{self.path.resolve().as_uri()}?mode=ro"
        connection: aiosqlite.Connection | None = None
        try:
            connection = await aiosqlite.connect(uri, uri=True)
            connection.row_factory = aiosqlite.Row
            await connection.execute("PRAGMA query_only=ON")
            await connection.execute("PRAGMA busy_timeout=1000")
            cursor = await connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
            tables = {str(row["name"]) for row in await cursor.fetchall()}
            await cursor.close()
            missing = REQUIRED_TABLES - tables
            if missing:
                raise DashboardDataError(
                    "database schema is incomplete: " + ", ".join(sorted(missing))
                )
        except (aiosqlite.Error, OSError) as exc:
            if connection is not None:
                await connection.close()
            raise DashboardDataError("database cannot be opened read-only") from exc
        except DashboardDataError:
            if connection is not None:
                await connection.close()
            raise
        self._connection = connection

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    async def snapshot(self, *, now: datetime | None = None) -> dict[str, object]:
        current_time = now or datetime.now(UTC)
        async with self._snapshot_lock:
            connection = self.connection
            try:
                await connection.execute("BEGIN")
                states = await self._risk_states()
                business = await self._state_group(
                    states, health=False, now=current_time
                )
                pillars = await self._critical_pillars(current_time)
                known_level = business_overall(
                    states,
                    now=current_time,
                    event_active_seconds=self.event_active_seconds,
                )
                assessment = assess_asset(known_level, pillars)
                business["level"] = assessment.level.name
                business["missing_pillars"] = [
                    {"name": pillar.name, "reason": pillar.reason}
                    for pillar in assessment.missing_pillars
                ]
                health = await self._state_group(
                    states, health=True, now=current_time
                )
                collectors, last_activity = await self._collector_health()
                health["collectors"] = collectors
                if last_activity is not None:
                    stale_at = last_activity + timedelta(
                        seconds=MONITOR_STALE_SECONDS
                    )
                    if current_time >= stale_at:
                        health["level"] = RiskLevel.RED.name
                        items = health["items"]
                        assert isinstance(items, list)
                        items.append(
                            {
                                "rule_id": "health.monitor_stale",
                                "level": RiskLevel.RED.name,
                                "summary": RULE_LABELS["health.monitor_stale"],
                                "first_triggered_at": self._format_time(stale_at),
                                "changed_at": self._format_time(stale_at),
                            }
                        )
                metrics = await self._metrics(current_time)
                coverage_available = next(
                    pillar.available for pillar in pillars if pillar.name == "coverage"
                )
                if not coverage_available:
                    metrics["estimated_collateralization"] = None
                result: dict[str, object] = {
                    "generated_at": self._format_time(current_time),
                    "business": business,
                    "health": health,
                    "metrics": metrics,
                    "custody": await self._custody(current_time),
                    "redemption": await self._redemption(current_time),
                    "recent": {
                        "alerts": await self._recent_alerts(),
                        "chain_events": await self._recent_chain_events(),
                        "announcements": await self._recent_announcements(),
                        "unverified_leads": await self._unverified_leads(),
                    },
                }
                await connection.commit()
                return result
            except BaseException as exc:
                await connection.rollback()
                if isinstance(exc, (aiosqlite.Error, ValueError)):
                    raise DashboardDataError(
                        "database snapshot cannot be read"
                    ) from exc
                raise

    async def _critical_pillars(self, now: datetime) -> list[Pillar]:
        return pillar_from_observations(
            now=now,
            coverage_max_age_seconds=self.coverage_max_age_seconds,
            concentration=await self._latest_observation(
                "custody.binance_share_lower_bound", "global"
            ),
            reserves=await self._latest_observation("por.reserves", "ethereum"),
            supply=await self._latest_observation(
                "supply.multichain_total", "global"
            ),
            redemption=await self._latest_observation(
                "redemption.channel_status", "global"
            ),
        )

    async def _latest_observation(
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
            metric=str(row["metric"]),
            source=str(row["source"]),
            scope=str(row["scope"]),
            value=float(row["value"]),
            unit=str(row["unit"]),
            observed_at=datetime.fromisoformat(row["observed_at"]),
            collected_at=datetime.fromisoformat(row["collected_at"]),
            quality=str(row["quality"]),
            metadata=json.loads(row["metadata_json"]),
        )

    async def _latest_fact_observation(
        self, metric: str, scope: str
    ) -> Observation | None:
        cursor = await self.connection.execute(
            """
            SELECT metric, source, scope, value, unit, observed_at,
                   collected_at, quality, metadata_json
            FROM observations
            WHERE metric = ? AND scope = ? AND quality = 'FACT'
            ORDER BY observed_at DESC, id DESC
            LIMIT 1
            """,
            (metric, scope),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return self._observation_from_row(row) if row is not None else None

    async def _risk_states(self) -> list[RiskState]:
        cursor = await self.connection.execute(
            """
            SELECT rule_id, level, first_triggered_at, changed_at
            FROM risk_states
            ORDER BY level DESC, rule_id
            """
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            RiskState(
                rule_id=str(row["rule_id"]),
                level=RiskLevel(int(row["level"])),
                first_triggered_at=datetime.fromisoformat(row["first_triggered_at"]),
                changed_at=datetime.fromisoformat(row["changed_at"]),
            )
            for row in rows
            if str(row["rule_id"]) not in LEGACY_HEALTH_RULE_IDS
        ]

    async def _state_group(
        self,
        states: list[RiskState],
        *,
        health: bool,
        now: datetime,
    ) -> dict[str, object]:
        selected = [
            state
            for state in states
            if is_monitoring_health_rule(state.rule_id) is health
        ]
        if not selected:
            return {"level": "UNKNOWN", "items": []}
        level = (
            health_overall(selected)
            if health
            else business_overall(
                selected,
                now=now,
                event_active_seconds=self.event_active_seconds,
            )
        )
        active = [state for state in selected if state.level > RiskLevel.GREEN]
        return {
            "level": level.name,
            "items": [await self._state_item(state) for state in active],
        }

    async def _state_item(self, state: RiskState) -> dict[str, object]:
        summary = await self._matching_alert_content(state)
        return {
            "rule_id": state.rule_id,
            "level": state.level.name,
            "summary": self._plain_alert_content(summary)
            or self._rule_label(state.rule_id),
            "first_triggered_at": self._format_time(state.first_triggered_at),
            "changed_at": self._format_time(state.changed_at),
        }

    async def _matching_alert_content(self, state: RiskState) -> str | None:
        cursor = await self.connection.execute(
            """
            SELECT content
            FROM alert_deliveries
            WHERE created_at = ?
              AND status != 'CANCELLED'
              AND instr(alert_key, ?) > 0
            ORDER BY id
            LIMIT 1
            """,
            (state.changed_at.isoformat(), f":{state.rule_id}:"),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return sanitize_dashboard_text(str(row["content"])) if row is not None else None

    async def _collector_health(
        self,
    ) -> tuple[list[dict[str, object]], datetime | None]:
        cursor = await self.connection.execute(
            """
            SELECT collector_id, consecutive_failures, last_success_at,
                   last_failure_at, last_error
            FROM collector_health
            ORDER BY consecutive_failures DESC, collector_id
            """
        )
        rows = await cursor.fetchall()
        await cursor.close()
        database_values = {
            str(self.path),
            str(self.path.resolve()),
            self.path.as_posix(),
            self.path.resolve().as_posix(),
        }
        result: list[dict[str, object]] = []
        activities: list[datetime] = []
        for row in rows:
            if str(row["collector_id"]) in LEGACY_COLLECTOR_IDS:
                continue
            error = str(row["last_error"]) if row["last_error"] else None
            if error is not None:
                for database_path in database_values:
                    error = error.replace(database_path, "[database]")
            last_success_at = (
                datetime.fromisoformat(row["last_success_at"])
                if row["last_success_at"]
                else None
            )
            last_failure_at = (
                datetime.fromisoformat(row["last_failure_at"])
                if row["last_failure_at"]
                else None
            )
            activities.extend(
                value
                for value in (last_success_at, last_failure_at)
                if value is not None
            )
            result.append(
                {
                    "collector_id": str(row["collector_id"]),
                    "consecutive_failures": int(row["consecutive_failures"]),
                    "last_success_at": (
                        self._format_time(last_success_at)
                        if last_success_at is not None
                        else None
                    ),
                    "last_failure_at": (
                        self._format_time(last_failure_at)
                        if last_failure_at is not None
                        else None
                    ),
                    "last_error": sanitize_dashboard_error(error),
                }
            )
        return result, max(activities, default=None)

    async def _recent_alerts(self) -> list[dict[str, object]]:
        cursor = await self.connection.execute(
            """
            SELECT alert_key, content, created_at, delivered_at, status
            FROM alert_deliveries
            WHERE status != 'CANCELLED'
              AND instr(alert_key, ':health.supply_ethereum:') = 0
              AND instr(alert_key, ':health.supply_bsc:') = 0
            ORDER BY created_at DESC, id ASC
            LIMIT ?
            """,
            (20,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        groups: dict[str, dict[str, object]] = {}
        status_priority = {"FAILED": 4, "IN_FLIGHT": 3, "PENDING": 2, "SENT": 1}
        for row in rows:
            key = base_alert_key(str(row["alert_key"]))
            item = groups.setdefault(
                key,
                {
                    "alert_key": key,
                    "parts": [],
                    "created_at": self._format_time(
                        datetime.fromisoformat(row["created_at"])
                    ),
                    "delivered_at": self._optional_time(row["delivered_at"]),
                    "status": str(row["status"]),
                },
            )
            parts = item["parts"]
            if isinstance(parts, list):
                parts.append(str(row["content"]))
            if status_priority.get(str(row["status"]), 0) > status_priority.get(
                str(item["status"]), 0
            ):
                item["status"] = str(row["status"])
            if item["delivered_at"] is None:
                item["delivered_at"] = self._optional_time(row["delivered_at"])
        result: list[dict[str, object]] = []
        for item in groups.values():
            parts = item.pop("parts")
            content = sanitize_dashboard_text("\n".join(parts))
            item["content"] = self._plain_alert_content(content)
            result.append(item)
            if len(result) == 10:
                break
        return result

    async def _recent_chain_events(self) -> list[dict[str, object]]:
        cursor = await self.connection.execute(
            """
            SELECT chain, block_number, tx_hash, event_type, observed_at
            FROM chain_events
            ORDER BY observed_at DESC, id DESC
            LIMIT ?
            """,
            (10,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        explorer_hosts = {
            "ethereum": "https://etherscan.io/tx/",
            "bsc": "https://bscscan.com/tx/",
        }
        return [
            {
                "chain": str(row["chain"]),
                "block_number": int(row["block_number"]),
                "tx_hash": str(row["tx_hash"]),
                "event_type": str(row["event_type"]),
                "observed_at": self._format_time(
                    datetime.fromisoformat(row["observed_at"])
                ),
                "url": (
                    f"{explorer_hosts[str(row['chain'])]}{row['tx_hash']}"
                    if str(row["chain"]) in explorer_hosts
                    else None
                ),
            }
            for row in rows
        ]

    async def _recent_announcements(self) -> list[dict[str, object]]:
        placeholders = ", ".join("?" for _ in OFFICIAL_ANNOUNCEMENT_SOURCES)
        cursor = await self.connection.execute(
            f"""
            SELECT source, title, url, published_at, first_seen_at
            FROM announcements
            WHERE source IN ({placeholders})
               OR (
                    source GLOB 'redemption_page_[0-9]*'
                    AND source NOT GLOB 'redemption_page_*[^0-9]*'
               )
            ORDER BY COALESCE(published_at, first_seen_at) DESC, id DESC
            LIMIT ?
            """,
            (*OFFICIAL_ANNOUNCEMENT_SOURCES, 10),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            {
                "source": str(row["source"]),
                "title": str(row["title"]),
                "url": safe_external_url(row["url"]),
                "published_at": self._optional_time(row["published_at"]),
                "first_seen_at": self._format_time(
                    datetime.fromisoformat(row["first_seen_at"])
                ),
            }
            for row in rows
        ]

    async def _unverified_leads(self) -> list[dict[str, object]]:
        cursor = await self.connection.execute(
            """
            SELECT source, title, url, published_at, first_seen_at, metadata_json
            FROM announcements
            WHERE source = 'media_redemption'
            ORDER BY COALESCE(published_at, first_seen_at) DESC, id DESC
            LIMIT ?
            """,
            (10,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        result: list[dict[str, object]] = []
        for row in rows:
            metadata = json.loads(row["metadata_json"])
            if not isinstance(metadata, dict):
                raise ValueError("media metadata must be an object")
            safe_url = safe_external_url(row["url"])
            publisher = urlsplit(safe_url).hostname if safe_url is not None else None
            result.append(
                {
                    "source": "media_redemption",
                    "publisher": publisher,
                    "title": sanitize_dashboard_text(str(row["title"])) or "",
                    "summary": sanitize_dashboard_text(
                        str(metadata.get("summary", ""))
                    )
                    or "",
                    "url": safe_url,
                    "published_at": self._optional_time(row["published_at"]),
                    "first_seen_at": self._format_time(
                        datetime.fromisoformat(row["first_seen_at"])
                    ),
                    "verified": False,
                }
            )
        return result

    async def _custody(self, now: datetime) -> dict[str, object]:
        latest_share = await self._latest_observation(
            "custody.binance_share_lower_bound", "global"
        )
        latest_share_is_fact = (
            latest_share is not None and latest_share.quality == "FACT"
        )
        share = (
            latest_share
            if latest_share_is_fact
            else await self._latest_fact_observation(
                "custody.binance_share_lower_bound", "global"
            )
        )
        addresses, address_cohort_complete, address_cohort_known = (
            await self._custody_addresses(
            now, share
            )
        )
        share_available = (
            share is not None
            and latest_share_is_fact
            and self._is_fresh_with_metadata(share, now)
            and address_cohort_complete
        )
        if not share_available:
            for item in addresses:
                item["available"] = False
                item["balance"] = None
        share_value = share.value if share is not None else None
        verified_balance = self._finite_metadata_number(
            share.metadata.get("verified_balance") if share is not None else None
        )
        observed_at = (
            self._format_time(share.observed_at) if share is not None else None
        )

        entities = {
            "binance_cex": self._address_group(
                addresses,
                lambda item: item["entity"] == "binance_cex"
                and item["status"] == "trusted",
                cohort_available=share_available,
                cohort_known=address_cohort_known,
            ),
            "binance_peg_reserve": self._address_group(
                addresses,
                lambda item: item["entity"] == "binance_peg_reserve"
                and item["status"] == "trusted",
                cohort_available=share_available,
                cohort_known=address_cohort_known,
            ),
            "candidate": self._address_group(
                addresses,
                lambda item: item["status"] == "candidate",
                cohort_available=share_available,
                cohort_known=address_cohort_known,
            ),
            "fireblocks_custody": self._address_group(
                addresses,
                lambda item: item["entity"] == "fireblocks_custody"
                and item["status"] == "trusted",
                cohort_available=share_available,
                cohort_known=address_cohort_known,
            ),
            "bitgo_issuer": self._address_group(
                addresses,
                lambda item: item["entity"] == "bitgo_issuer"
                and item["status"] == "trusted",
                cohort_available=share_available,
                cohort_known=address_cohort_known,
            ),
            "unlabeled_whale": self._address_group(
                addresses,
                lambda item: item["entity"] == "unlabeled_whale",
                cohort_available=share_available,
                cohort_known=address_cohort_known,
            ),
        }
        net_change = await self._dashboard_flow_observation(
            "custody.binance_net_change_24h", "global", now
        )
        largest_outflow = await self._largest_address_outflow(now, addresses)
        solana = await self._solana_deltas(now, addresses)
        last_known = None
        if share is not None and address_cohort_known:
            last_known = {
                "binance_share_lower_bound": share_value,
                "binance_verified_balance": verified_balance,
                "observed_at": observed_at,
            }
        return {
            "available": share_available,
            "label": "已核验地址至少占比",
            "binance_share_lower_bound": share_value if share_available else None,
            "binance_verified_balance": (
                verified_balance if share_available else None
            ),
            "observed_at": observed_at if share_available else None,
            "last_known": None if share_available else last_known,
            "entities": entities,
            "binance_net_change_24h": net_change,
            "largest_address_outflow_1h": largest_outflow,
            "solana": solana,
            "addresses": addresses,
        }

    async def _custody_addresses(
        self, now: datetime, share: Observation | None
    ) -> tuple[list[dict[str, object]], bool, bool]:
        if share is None:
            return [], False, False
        rows = await self._observation_rows_at(
            "custody.address_balance", share.observed_at
        )
        observations = [self._observation_from_row(row) for row in rows]
        newest_at = await self._latest_metric_time("custody.address_balance")
        cohort_known = bool(observations) and all(
            item.quality == "FACT" for item in observations
        )

        safe_blocks: dict[str, set[int]] = {}
        for observation in observations:
            if ":" not in observation.scope:
                raise ValueError("custody address scope is malformed")
            chain = observation.scope.split(":", 1)[0]
            safe_block = observation.metadata.get("safe_block")
            if (
                isinstance(safe_block, bool)
                or not isinstance(safe_block, int)
                or safe_block < 0
            ):
                cohort_known = False
            else:
                safe_blocks.setdefault(chain, set()).add(safe_block)
        if any(len(values) != 1 for values in safe_blocks.values()):
            cohort_known = False

        expected_balance = self._finite_metadata_number(
            share.metadata.get("verified_balance")
        )
        actual_balance = sum(
            item.value
            for item in observations
            if item.metadata.get("status") == "trusted"
            and item.metadata.get("entity")
            in {"binance_cex", "binance_peg_reserve"}
        )
        if expected_balance is None or not math.isclose(
            actual_balance, expected_balance, rel_tol=1e-12, abs_tol=1e-6
        ):
            cohort_known = False

        coherent = cohort_known and not (
            newest_at is not None and newest_at > share.observed_at
        )

        result: list[dict[str, object]] = []
        for observation in observations:
            if ":" not in observation.scope:
                raise ValueError("custody address scope is malformed")
            chain, address = observation.scope.split(":", 1)
            metadata = observation.metadata
            if not isinstance(metadata, dict):
                raise ValueError("custody address metadata must be an object")
            raw_urls = metadata.get("evidence_urls", [])
            if not isinstance(raw_urls, list):
                raise ValueError("custody evidence_urls must be a list")
            available = coherent and self._is_fresh_with_metadata(observation, now)
            item: dict[str, object] = {
                "chain": sanitize_dashboard_text(chain) or "",
                "address": sanitize_dashboard_text(address) or "",
                "entity": sanitize_dashboard_text(str(metadata.get("entity", "")))
                or "",
                "label": sanitize_dashboard_text(str(metadata.get("label", address)))
                or address,
                "status": sanitize_dashboard_text(
                    str(metadata.get("status", "candidate"))
                )
                or "candidate",
                "evidence_urls": [
                    safe_url
                    for value in raw_urls
                    if (safe_url := safe_external_url(value)) is not None
                ],
                "verified_on": (
                    sanitize_dashboard_text(str(metadata["verified_on"]))
                    if metadata.get("verified_on") is not None
                    else None
                ),
                "available": available,
                "balance": observation.value if available else None,
                "last_known_balance": observation.value,
                "observed_at": self._format_time(observation.observed_at),
            }
            result.append(item)
        order = {
            "binance_cex": 0,
            "binance_peg_reserve": 1,
            "fireblocks_custody": 2,
            "bitgo_issuer": 3,
            "unlabeled_whale": 4,
        }
        return sorted(
            result,
            key=lambda item: (
                item["status"] != "trusted",
                order.get(str(item["entity"]), 9),
                str(item["chain"]),
                str(item["label"]),
                str(item["address"]),
            ),
        ), coherent, cohort_known

    @staticmethod
    def _address_group(
        addresses: list[dict[str, object]],
        predicate: Callable[[dict[str, object]], bool],
        *,
        cohort_available: bool,
        cohort_known: bool,
    ) -> dict[str, object]:
        selected = [item for item in addresses if predicate(item)]
        available = (
            cohort_available
            and bool(selected)
            and all(bool(item["available"]) for item in selected)
        )
        last_known = sum(float(item["last_known_balance"]) for item in selected)
        return {
            "available": available,
            "balance": last_known if available else None,
            "last_known_balance": last_known if selected and cohort_known else None,
            "address_count": len(selected),
        }

    async def _dashboard_flow_observation(
        self, metric: str, scope: str, now: datetime
    ) -> dict[str, object]:
        expected_window_hours = FLOW_WINDOW_HOURS[metric]
        observation = await self._latest_observation(metric, scope)
        last_known = None
        if observation is not None and not self._is_complete_flow_raw(
            observation, expected_window_hours
        ):
            last_known = await self._latest_complete_flow(
                metric,
                scope,
                before=observation.observed_at,
                expected_window_hours=expected_window_hours,
            )
        return self._flow_payload(
            observation,
            now,
            expected_window_hours=expected_window_hours,
            last_known=last_known,
        )

    async def _largest_address_outflow(
        self, now: datetime, addresses: list[dict[str, object]]
    ) -> dict[str, object]:
        metric = "custody.address_external_outflow_1h"
        expected_window_hours = FLOW_WINDOW_HOURS[metric]
        observations = await self._latest_metric_cohort(metric)
        expected_scopes = {
            f"{item['chain']}:{item['address']}".casefold()
            for item in addresses
            if item["chain"] in {"ethereum", "bsc"}
            and item["status"] == "trusted"
            and item["entity"] in {"binance_cex", "binance_peg_reserve"}
        }
        current_complete = bool(observations) and all(
            self._is_complete_flow_raw(item, expected_window_hours)
            for item in observations
        ) and (
            not expected_scopes
            or {item.scope.casefold() for item in observations} == expected_scopes
        )
        current = max(observations, key=lambda item: item.value, default=None)
        if current_complete and current is not None:
            payload = self._flow_payload(
                current, now, expected_window_hours=expected_window_hours
            )
            self._add_address_identity(payload, current.scope, addresses)
            return payload
        before = observations[0].observed_at if observations else None
        last_known_cohort = await self._latest_complete_cohort(
            metric,
            before=before,
            expected_scopes=expected_scopes,
            expected_window_hours=expected_window_hours,
        )
        last_known = max(last_known_cohort, key=lambda item: item.value, default=None)
        payload = self._flow_payload(
            None,
            now,
            expected_window_hours=expected_window_hours,
            last_known=last_known,
        )
        if last_known is not None:
            self._add_address_identity(payload, last_known.scope, addresses)
        return payload

    @staticmethod
    def _add_address_identity(
        payload: dict[str, object],
        scope: str,
        addresses: list[dict[str, object]],
    ) -> None:
        safe_scope = sanitize_dashboard_text(scope) or ""
        payload["scope"] = safe_scope
        matched = next(
            (
                item
                for item in addresses
                if f"{item['chain']}:{item['address']}".casefold()
                == safe_scope.casefold()
            ),
            None,
        )
        if matched is not None:
            payload["chain"] = matched["chain"]
            payload["address"] = matched["address"]
            payload["label"] = matched["label"]
            payload["status"] = matched["status"]

    async def _solana_deltas(
        self, now: datetime, addresses: list[dict[str, object]]
    ) -> dict[str, object]:
        result: dict[str, object] = {"counterparty_attribution": False}
        expected_scopes = {
            f"solana:{item['address']}".casefold()
            for item in addresses
            if item["chain"] == "solana" and item["status"] == "trusted"
        }
        for hours in (1, 24):
            metric = f"custody.solana_balance_delta_{hours}h"
            expected_window_hours = FLOW_WINDOW_HOURS[metric]
            observations = await self._latest_metric_cohort(metric)
            key = f"delta_{hours}h"
            raw_complete = bool(observations) and all(
                self._is_complete_flow_raw(item, expected_window_hours)
                for item in observations
            )
            previous = await self._latest_complete_cohort(
                metric,
                before=observations[0].observed_at if observations else None,
                expected_scopes=expected_scopes,
                expected_window_hours=expected_window_hours,
            )
            current_scopes = {item.scope.casefold() for item in observations}
            same_scope_set = (
                current_scopes == expected_scopes
                if expected_scopes
                else not previous
                or current_scopes
                == {item.scope.casefold() for item in previous}
            )
            available = (
                raw_complete
                and same_scope_set
                and all(
                    self._is_complete_flow(item, now, expected_window_hours)
                    for item in observations
                )
            )
            result[key] = (
                sum(item.value for item in observations) if available else None
            )
            result[f"{key}_available"] = available
            if not available:
                last_known = (
                    observations if raw_complete and same_scope_set else previous
                )
                if last_known and all(
                    self._is_complete_flow_raw(item, expected_window_hours)
                    for item in last_known
                ):
                    result[f"{key}_last_known"] = sum(
                        item.value for item in last_known
                    )
        return result

    async def _redemption(self, now: datetime) -> dict[str, object]:
        latest = await self._latest_observation(
            "redemption.channel_status", "global"
        )
        latest_is_fact = latest is not None and latest.quality == "FACT"
        observation = (
            latest
            if latest_is_fact
            else await self._latest_fact_observation(
                "redemption.channel_status", "global"
            )
        )
        if observation is None:
            return {
                "available": False,
                "level": "UNKNOWN",
                "summary": "官方赎回通道数据暂不可用",
                "source_url": None,
                "observed_at": None,
                "last_known": None,
            }
        raw_level = int(observation.value)
        if observation.value != raw_level or raw_level not in {0, 1, 2}:
            raise ValueError("redemption channel level is malformed")
        level = RiskLevel(raw_level).name
        raw_summary = observation.metadata.get("summary")
        summary = sanitize_dashboard_text(
            str(raw_summary) if raw_summary is not None else None
        )
        if not summary and level == "GREEN":
            summary = "未发现官方限制"
        if not summary:
            summary = "官方赎回通道状态发生变化"
        source_url = safe_external_url(observation.metadata.get("source_url"))
        observed_at = self._format_time(observation.observed_at)
        available = latest_is_fact and self._is_fresh_with_metadata(
            observation, now
        )
        last_known = {
            "level": level,
            "summary": summary,
            "source_url": source_url,
            "observed_at": observed_at,
        }
        return {
            "available": available,
            "level": level if available else "UNKNOWN",
            "summary": summary if available else "官方赎回通道数据暂不可用",
            "source_url": source_url if available else None,
            "observed_at": observed_at if available else None,
            "last_known": None if available else last_known,
        }

    async def _latest_metric_time(
        self, metric: str, *, before: datetime | None = None
    ) -> datetime | None:
        condition = " AND observed_at < ?" if before is not None else ""
        params: tuple[object, ...] = (
            (metric, before.isoformat()) if before is not None else (metric,)
        )
        cursor = await self.connection.execute(
            f"""
            SELECT observed_at
            FROM observations
            WHERE metric = ?{condition}
            ORDER BY observed_at DESC
            LIMIT 1
            """,
            params,
        )
        row = await cursor.fetchone()
        await cursor.close()
        return datetime.fromisoformat(row["observed_at"]) if row is not None else None

    async def _observation_rows_at(
        self, metric: str, observed_at: datetime
    ) -> list[aiosqlite.Row]:
        cursor = await self.connection.execute(
            """
            SELECT metric, source, scope, value, unit, observed_at,
                   collected_at, quality, metadata_json
            FROM observations
            WHERE metric = ? AND observed_at = ?
            ORDER BY id DESC
            """,
            (metric, observed_at.isoformat()),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        latest_by_scope: dict[str, aiosqlite.Row] = {}
        for row in rows:
            latest_by_scope.setdefault(str(row["scope"]), row)
        return list(latest_by_scope.values())

    async def _latest_metric_cohort(self, metric: str) -> list[Observation]:
        observed_at = await self._latest_metric_time(metric)
        if observed_at is None:
            return []
        return [
            self._observation_from_row(row)
            for row in await self._observation_rows_at(metric, observed_at)
        ]

    async def _latest_complete_flow(
        self,
        metric: str,
        scope: str,
        *,
        before: datetime,
        expected_window_hours: int,
    ) -> Observation | None:
        cohort = await self._latest_complete_cohort(
            metric,
            before=before,
            expected_scopes={scope.casefold()},
            expected_window_hours=expected_window_hours,
        )
        return cohort[0] if cohort else None

    async def _latest_complete_cohort(
        self,
        metric: str,
        *,
        before: datetime | None,
        expected_scopes: set[str] | None = None,
        expected_window_hours: int,
    ) -> list[Observation]:
        condition = " AND observed_at < ?" if before is not None else ""
        params: tuple[object, ...] = (
            (metric, before.isoformat()) if before is not None else (metric,)
        )
        cursor = await self.connection.execute(
            f"""
            SELECT metric, source, scope, value, unit, observed_at,
                   collected_at, quality, metadata_json
            FROM observations
            WHERE metric = ?{condition}
            ORDER BY observed_at DESC, id DESC
            LIMIT {FLOW_COHORT_CANDIDATE_LIMIT + 1}
            """,
            params,
        )
        rows = await cursor.fetchall()
        await cursor.close()
        truncated_time = (
            str(rows[FLOW_COHORT_CANDIDATE_LIMIT]["observed_at"])
            if len(rows) > FLOW_COHORT_CANDIDATE_LIMIT
            else None
        )
        normalized_expected_scopes = (
            {scope.casefold() for scope in expected_scopes}
            if expected_scopes is not None
            else None
        )
        cohorts: dict[str, dict[str, Observation]] = {}
        for row in rows[:FLOW_COHORT_CANDIDATE_LIMIT]:
            observation = self._observation_from_row(row)
            by_scope = cohorts.setdefault(str(row["observed_at"]), {})
            by_scope.setdefault(observation.scope.casefold(), observation)
        for observed_at, by_scope in cohorts.items():
            if observed_at == truncated_time:
                continue
            cohort = list(by_scope.values())
            if (
                all(
                    self._is_complete_flow_raw(item, expected_window_hours)
                    for item in cohort
                )
                and (
                    not normalized_expected_scopes
                    or set(by_scope) == normalized_expected_scopes
                )
            ):
                return cohort
        return []

    @staticmethod
    def _observation_from_row(row: aiosqlite.Row) -> Observation:
        return Observation(
            metric=str(row["metric"]),
            source=str(row["source"]),
            scope=str(row["scope"]),
            value=float(row["value"]),
            unit=str(row["unit"]),
            observed_at=datetime.fromisoformat(row["observed_at"]),
            collected_at=datetime.fromisoformat(row["collected_at"]),
            quality=str(row["quality"]),
            metadata=json.loads(row["metadata_json"]),
        )

    @staticmethod
    def _finite_metadata_number(value: object) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        number = float(value)
        return number if math.isfinite(number) else None

    @staticmethod
    def _observation_age_seconds(observation: Observation, now: datetime) -> float:
        if (
            observation.observed_at.tzinfo is None
            or observation.observed_at.utcoffset() is None
        ):
            raise ValueError("observation timestamp must be timezone-aware")
        age = (
            now.astimezone(UTC) - observation.observed_at.astimezone(UTC)
        ).total_seconds()
        if age < 0:
            raise ValueError("observation timestamp must not be in the future")
        return age

    @classmethod
    def _is_fresh_with_metadata(
        cls, observation: Observation | None, now: datetime
    ) -> bool:
        if observation is None or not isinstance(observation.metadata, dict):
            return False
        max_age = observation.metadata.get("max_age_seconds")
        if type(max_age) is not int or max_age <= 0:
            return False
        return cls._observation_age_seconds(observation, now) < max_age

    @classmethod
    def _is_complete_flow(
        cls,
        observation: Observation,
        now: datetime,
        expected_window_hours: int,
    ) -> bool:
        return (
            cls._is_complete_flow_raw(observation, expected_window_hours)
            and cls._observation_age_seconds(observation, now) < 1200
        )

    @staticmethod
    def _is_complete_flow_raw(
        observation: Observation, expected_window_hours: int
    ) -> bool:
        window_hours = observation.metadata.get("window_hours")
        return (
            observation.quality == "FACT"
            and observation.metadata.get("status") == "complete"
            and type(window_hours) is int
            and window_hours == expected_window_hours
        )

    @classmethod
    def _flow_payload(
        cls,
        observation: Observation | None,
        now: datetime,
        *,
        expected_window_hours: int,
        last_known: Observation | None = None,
    ) -> dict[str, object]:
        available = observation is not None and cls._is_complete_flow(
            observation, now, expected_window_hours
        )
        known = (
            observation
            if observation is not None
            and cls._is_complete_flow_raw(observation, expected_window_hours)
            else last_known
        )
        last_known_payload = (
            {
                "value": known.value,
                "observed_at": known.observed_at.isoformat(),
            }
            if known is not None
            and cls._is_complete_flow_raw(known, expected_window_hours)
            else None
        )
        return {
            "available": available,
            "value": observation.value if available and observation is not None else None,
            "last_known": None if available else last_known_payload,
            "observed_at": (
                observation.observed_at.isoformat()
                if available and observation is not None
                else None
            ),
        }

    async def _metrics(self, now: datetime) -> dict[str, object | None]:
        metrics: dict[str, object | None] = {
            key: None for key in (*METRIC_KEYS.values(), "supply_change_24h")
        }
        for (metric, scope), output_key in METRIC_KEYS.items():
            cursor = await self.connection.execute(
                """
                SELECT metric, scope, value, unit, observed_at, collected_at,
                       quality, metadata_json
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
                continue
            item: dict[str, object] = {
                "value": float(row["value"]),
                "unit": str(row["unit"]),
                "quality": str(row["quality"]),
                "observed_at": self._format_time(
                    datetime.fromisoformat(row["observed_at"])
                ),
                "collected_at": self._format_time(
                    datetime.fromisoformat(row["collected_at"])
                ),
            }
            if output_key.startswith("exit_"):
                metadata = json.loads(row["metadata_json"])
                item["fully_fillable"] = bool(metadata.get("fully_fillable", False))
            if output_key == "reserves":
                observed_at = datetime.fromisoformat(str(row["observed_at"]))
                if observed_at.tzinfo is None or observed_at.utcoffset() is None:
                    raise ValueError("observation timestamp must be timezone-aware")
                reserve_age = (
                    now.astimezone(UTC) - observed_at.astimezone(UTC)
                ).total_seconds()
                if reserve_age < 0:
                    raise ValueError("observation timestamp must not be in the future")
                reserve_available = (
                    str(row["quality"]) == "FACT"
                    and reserve_age < self.coverage_max_age_seconds
                )
                item["available"] = reserve_available
                item["last_known"] = not reserve_available
            metrics[output_key] = item
        metrics["supply_change_24h"] = await self._supply_change_24h(
            now, metrics["multichain_supply"]
        )
        return metrics

    def _plain_alert_content(self, content: str | None) -> str | None:
        if content is None or not ("当前值=" in content and "阈值=" in content):
            return content
        labels: list[str] = []
        for rule_id in LEGACY_RULE_PATTERN.findall(content):
            label = self._rule_label(rule_id)
            if label not in labels:
                labels.append(label)
        return "；".join(labels) if labels else "监控状态发生变化"

    async def _supply_change_24h(
        self, now: datetime, current: object | None
    ) -> dict[str, object] | None:
        if not isinstance(current, dict):
            return None
        current_value = float(current["value"])
        if current_value <= 0:
            return None
        current_at = datetime.fromisoformat(str(current["observed_at"]))
        if now - current_at > timedelta(seconds=4500):
            return None
        cutoff = now - timedelta(hours=24)
        cursor = await self.connection.execute(
            """
            SELECT value, observed_at
            FROM observations
            WHERE metric = 'supply.multichain_total'
              AND scope = 'global'
              AND observed_at <= ?
            ORDER BY observed_at DESC, id DESC
            LIMIT 1
            """,
            (cutoff.isoformat(),),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        baseline_at = datetime.fromisoformat(row["observed_at"])
        if cutoff - baseline_at > timedelta(seconds=4500):
            return None
        baseline = float(row["value"])
        if baseline <= 0:
            return None
        return {
            "value": (current_value / baseline - 1) * 100,
            "unit": "percent",
            "quality": "CALCULATED",
            "observed_at": current["observed_at"],
            "collected_at": current["collected_at"],
        }

    def _rule_label(self, rule_id: str) -> str:
        if rule_id in RULE_LABELS:
            return RULE_LABELS[rule_id]
        prefixes = (
            ("expiry.event.information.", "官方信息提醒已结束"),
            ("event.information.", "官方信息出现需要关注的变化"),
            ("market.", "USD1 市场指标发生变化"),
            ("por.", "USD1 储备指标发生变化"),
            ("supply.", "USD1 供应量指标发生变化"),
            ("evm.", "USD1 链上合约状态发生变化"),
            ("information.", "官方信息出现需要关注的变化"),
            ("health.", "监控数据源发生异常"),
        )
        for prefix, label in prefixes:
            if rule_id.startswith(prefix):
                return label
        return "监控规则发生变化"

    def _format_time(self, value: datetime) -> str:
        return local_iso(value, self.timezone_name)

    def _optional_time(self, value: object) -> str | None:
        if not value:
            return None
        return self._format_time(datetime.fromisoformat(str(value)))
