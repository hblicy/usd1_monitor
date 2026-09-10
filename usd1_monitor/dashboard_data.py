from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
import aiosqlite

from usd1_monitor.engine.aggregate import business_overall, health_overall
from usd1_monitor.http import sanitize_url
from usd1_monitor.models import RiskLevel, RiskState
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
    ) -> None:
        self.path = path
        self.timezone_name = timezone_name
        self.event_active_seconds = event_active_seconds
        self._connection: aiosqlite.Connection | None = None

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
        connection = self.connection
        try:
            await connection.execute("BEGIN")
            states = await self._risk_states()
            business = await self._state_group(states, health=False, now=current_time)
            health = await self._state_group(states, health=True, now=current_time)
            health["collectors"] = await self._collector_health()
            result: dict[str, object] = {
                "generated_at": self._format_time(current_time),
                "business": business,
                "health": health,
                "metrics": await self._metrics(current_time),
                "recent": {
                    "alerts": await self._recent_alerts(),
                    "chain_events": await self._recent_chain_events(),
                    "announcements": await self._recent_announcements(),
                },
            }
            await connection.commit()
            return result
        except (aiosqlite.Error, ValueError) as exc:
            await connection.rollback()
            raise DashboardDataError("database snapshot cannot be read") from exc

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
            if state.rule_id.startswith("health.") is health
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

    async def _collector_health(self) -> list[dict[str, object]]:
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
        for row in rows:
            error = str(row["last_error"]) if row["last_error"] else None
            if error is not None:
                for database_path in database_values:
                    error = error.replace(database_path, "[database]")
            result.append(
                {
                    "collector_id": str(row["collector_id"]),
                    "consecutive_failures": int(row["consecutive_failures"]),
                    "last_success_at": self._optional_time(row["last_success_at"]),
                    "last_failure_at": self._optional_time(row["last_failure_at"]),
                    "last_error": sanitize_dashboard_error(error),
                }
            )
        return result

    async def _recent_alerts(self) -> list[dict[str, object]]:
        cursor = await self.connection.execute(
            """
            SELECT alert_key, content, created_at, delivered_at, status
            FROM alert_deliveries
            WHERE status != 'CANCELLED'
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
        cursor = await self.connection.execute(
            """
            SELECT source, title, url, published_at, first_seen_at
            FROM announcements
            ORDER BY COALESCE(published_at, first_seen_at) DESC, id DESC
            LIMIT ?
            """,
            (10,),
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
