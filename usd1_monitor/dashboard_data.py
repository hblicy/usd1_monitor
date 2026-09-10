from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import aiosqlite

from usd1_monitor.engine.aggregate import business_overall, health_overall
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
            result: dict[str, object] = {
                "generated_at": self._format_time(current_time),
                "business": await self._state_group(states, health=False, now=current_time),
                "health": await self._state_group(states, health=True, now=current_time),
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
            "summary": summary or self._rule_label(state.rule_id),
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
              AND alert_key LIKE ?
            ORDER BY id
            LIMIT 1
            """,
            (state.changed_at.isoformat(), f"%:{state.rule_id}:%"),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return str(row["content"]) if row is not None else None

    def _rule_label(self, rule_id: str) -> str:
        if rule_id in RULE_LABELS:
            return RULE_LABELS[rule_id]
        prefixes = (
            ("market.", "USD1 市场指标发生变化"),
            ("por.", "USD1 储备指标发生变化"),
            ("supply.", "USD1 供应量指标发生变化"),
            ("evm.", "USD1 链上合约状态发生变化"),
            ("health.", "监控数据源发生异常"),
        )
        for prefix, label in prefixes:
            if rule_id.startswith(prefix):
                return label
        return "监控规则发生变化"

    def _format_time(self, value: datetime) -> str:
        return local_iso(value, self.timezone_name)
