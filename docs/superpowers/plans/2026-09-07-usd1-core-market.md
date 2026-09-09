# USD1 Core and Market Monitoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the runnable USD1 monitor foundation with validated configuration, SQLite persistence, Binance public order-book monitoring, market risk state transitions, collector-health alerts, WeChat delivery, and CLI commands.

**Architecture:** A single Python 3.11+ asyncio process runs independent collectors. Collectors emit normalized observations; pure rules evaluate them; SQLite persists observations, states, cursors, and delivery results; notification delivery occurs only after a committed state transition.

**Tech Stack:** Python 3.11+, aiohttp, aiosqlite, Pydantic 2, PyYAML, python-dotenv, pytest, pytest-asyncio, system SQLite

---

Run all commands from `07-web3-bot/DEFI/usd1_monitor`.

### Task 1: Bootstrap the package and strict configuration

**Files:**
- Create: `requirements.txt`
- Create: `requirements-dev.txt`
- Create: `.gitignore`
- Create: `.env.example`
- Create: `config.example.yaml`
- Create: `usd1_monitor/__init__.py`
- Create: `usd1_monitor/config.py`
- Create: `tests/__init__.py`
- Create: `tests/test_config.py`
- Create: `pytest.ini`

- [ ] **Step 1: Write failing configuration tests**

```python
# tests/test_config.py
from pathlib import Path

import pytest

from usd1_monitor.config import ConfigError, load_config


def test_load_config_reads_market_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
database_path: data/monitor.db
timezone: Asia/Shanghai
market:
  symbols: [USD1USDT, USD1USDC]
  interval_seconds: 60
  depth_limit: 1000
  yellow_price: 0.997
  yellow_seconds: 900
  red_price: 0.995
  red_seconds: 300
  recovery_price: 0.998
  recovery_seconds: 900
  sell_sizes: [1000000, 5000000, 20000000]
http:
  timeout_seconds: 10
  retries: 2
""".strip(),
        encoding="utf-8",
    )

    config = load_config(path, environ={})

    assert config.market.symbols == ["USD1USDT", "USD1USDC"]
    assert config.market.depth_limit == 1000
    assert config.wechat_webhook is None


def test_load_config_rejects_inverted_market_thresholds(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
database_path: monitor.db
market:
  symbols: [USD1USDT, USD1USDC]
  yellow_price: 0.995
  red_price: 0.997
  recovery_price: 0.998
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="red_price must be lower"):
        load_config(path, environ={})
```

- [ ] **Step 2: Run the test and verify the package is missing**

Run: `python -m pytest tests/test_config.py -v`

Expected: collection fails with `ModuleNotFoundError: No module named 'usd1_monitor'`.

- [ ] **Step 3: Create dependencies and package metadata files**

```text
# requirements.txt
aiohttp>=3.10,<4
aiosqlite>=0.20,<1
pydantic>=2.10,<3
PyYAML>=6,<7
python-dotenv>=1,<2
```

```text
# requirements-dev.txt
-r requirements.txt
pytest>=8,<10
pytest-asyncio>=0.24,<2
```

```text
# pytest.ini
[pytest]
asyncio_mode = auto
testpaths = tests
```

```text
# .gitignore
.env
config.yaml
data/
logs/
*.db
*.db-shm
*.db-wal
__pycache__/
.pytest_cache/
```

```dotenv
# .env.example
WECHAT_WEBHOOK=
ETH_RPC_URLS=
BSC_RPC_URLS=
```

- [ ] **Step 4: Implement strict configuration loading**

Create `usd1_monitor/__init__.py` with `__version__ = "0.1.0"` and an empty `tests/__init__.py` so later plans can import shared deterministic fakes.

Create `usd1_monitor/config.py` with these public interfaces:

```python
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator


class ConfigError(ValueError):
    pass


class HttpConfig(BaseModel):
    timeout_seconds: float = Field(default=10, gt=0)
    retries: int = Field(default=2, ge=0, le=5)


class MarketConfig(BaseModel):
    symbols: list[str] = ["USD1USDT", "USD1USDC"]
    interval_seconds: int = Field(default=60, ge=10)
    depth_limit: int = Field(default=1000)
    yellow_price: float = 0.997
    yellow_seconds: int = 900
    red_price: float = 0.995
    red_seconds: int = 300
    recovery_price: float = 0.998
    recovery_seconds: int = 900
    sell_sizes: list[float] = [1_000_000, 5_000_000, 20_000_000]

    @model_validator(mode="after")
    def validate_threshold_order(self) -> "MarketConfig":
        if self.red_price >= self.yellow_price:
            raise ValueError("red_price must be lower than yellow_price")
        if self.recovery_price <= self.yellow_price:
            raise ValueError("recovery_price must be higher than yellow_price")
        if self.depth_limit != 1000:
            raise ValueError("depth_limit must be 1000 for the specified Binance snapshot")
        if self.symbols != ["USD1USDT", "USD1USDC"]:
            raise ValueError("symbols must be USD1USDT and USD1USDC")
        return self


class AppConfig(BaseModel):
    database_path: Path
    timezone: str = "Asia/Shanghai"
    market: MarketConfig = MarketConfig()
    http: HttpConfig = HttpConfig()
    wechat_webhook: str | None = None


def load_config(path: Path, environ: Mapping[str, str] | None = None) -> AppConfig:
    values = os.environ if environ is None else environ
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        raw["wechat_webhook"] = values.get("WECHAT_WEBHOOK") or None
        return AppConfig.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ConfigError(f"invalid configuration at {path}: {exc}") from exc
```

Create `config.example.yaml` using the passing fixture values plus `http.timeout_seconds: 10` and `http.retries: 2`.

- [ ] **Step 5: Run the configuration tests**

Run: `python -m pytest tests/test_config.py -v`

Expected: 2 tests pass.

- [ ] **Step 6: Commit the bootstrap**

```bash
git add 07-web3-bot/DEFI/usd1_monitor/.gitignore 07-web3-bot/DEFI/usd1_monitor/.env.example 07-web3-bot/DEFI/usd1_monitor/config.example.yaml 07-web3-bot/DEFI/usd1_monitor/requirements.txt 07-web3-bot/DEFI/usd1_monitor/requirements-dev.txt 07-web3-bot/DEFI/usd1_monitor/pytest.ini 07-web3-bot/DEFI/usd1_monitor/usd1_monitor/__init__.py 07-web3-bot/DEFI/usd1_monitor/usd1_monitor/config.py 07-web3-bot/DEFI/usd1_monitor/tests/__init__.py 07-web3-bot/DEFI/usd1_monitor/tests/test_config.py
git commit -m "功能：初始化 USD1 监控配置"
```

### Task 2: Define domain models and SQLite persistence

**Files:**
- Create: `usd1_monitor/models.py`
- Create: `usd1_monitor/storage.py`
- Create: `tests/conftest.py`
- Create: `tests/test_storage.py`

- [ ] **Step 1: Write failing persistence tests**

```python
# tests/test_storage.py
from datetime import UTC, datetime
from pathlib import Path

import pytest

from usd1_monitor.models import Observation, RiskLevel
from usd1_monitor.storage import Storage


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
    await storage.set_risk_state("market.price", RiskLevel.YELLOW, observed_at, observed_at)
    await storage.close()

    reopened = Storage(db_path)
    await reopened.open()
    assert (await reopened.get_risk_state("market.price")).level is RiskLevel.YELLOW
    assert len(await reopened.latest_observations("market.mid_price", limit=5)) == 1
    await reopened.close()


@pytest.mark.asyncio
async def test_prune_observations_keeps_risk_history(tmp_path: Path) -> None:
    db_path = tmp_path / "monitor.db"
    storage = Storage(db_path)
    await storage.open()
    await storage.insert_observation(
        Observation("market.mid_price", "binance", "USD1USDT", 1.0, "USDT", datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC))
    )
    await storage.set_risk_state("market.price", RiskLevel.GREEN, datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC))
    assert await storage.prune_observations(datetime(2026, 7, 1, tzinfo=UTC)) == 1
    assert await storage.get_risk_state("market.price") is not None
    await storage.close()
```

- [ ] **Step 2: Run the persistence test and verify failure**

Run: `python -m pytest tests/test_storage.py -v`

Expected: import fails because `usd1_monitor.models` and `usd1_monitor.storage` do not exist.

- [ ] **Step 3: Implement normalized domain models**

```python
# usd1_monitor/models.py
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum, StrEnum
from typing import Any


class RiskLevel(IntEnum):
    GREEN = 0
    YELLOW = 1
    RED = 2


class CoverageState(StrEnum):
    MONITORED = "MONITORED"
    UNKNOWN = "UNKNOWN"
    NOT_MONITORED = "NOT_MONITORED"


@dataclass(frozen=True)
class Observation:
    metric: str
    source: str
    scope: str
    value: float
    unit: str
    observed_at: datetime
    collected_at: datetime
    quality: str = "FACT"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RiskState:
    rule_id: str
    level: RiskLevel
    first_triggered_at: datetime
    changed_at: datetime
```

- [ ] **Step 4: Implement schema creation and the tested storage methods**

`Storage.open()` must create parent directories, enable WAL and foreign keys, and create `observations`, `risk_states`, `alert_deliveries`, `collector_health`, `chain_events`, `announcements`, and `scan_cursors`. Implement parameterized SQL only. `set_risk_state()` uses `INSERT ... ON CONFLICT(rule_id) DO UPDATE`; `get_risk_state()` converts stored ISO timestamps back to timezone-aware `datetime`; `latest_observations()` orders by `observed_at DESC`.

Implement `prune_observations(cutoff)` as one parameterized `DELETE` against `observations` only and return `cursor.rowcount`. The scheduler calls it once per day using the configured 180-day cutoff; event, announcement, risk-state, and delivery tables are not pruned.

The exact schema keys required in this phase are:

```sql
CREATE TABLE observations (
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
CREATE INDEX idx_observations_metric_time
ON observations(metric, observed_at DESC);
CREATE TABLE risk_states (
  rule_id TEXT PRIMARY KEY,
  level INTEGER NOT NULL,
  first_triggered_at TEXT NOT NULL,
  changed_at TEXT NOT NULL
);
```

- [ ] **Step 5: Run the persistence tests**

Create the shared temporary-storage fixture before running the tests:

```python
# tests/conftest.py
from pathlib import Path

import pytest_asyncio

from usd1_monitor.storage import Storage


@pytest_asyncio.fixture
async def storage(tmp_path: Path):
    instance = Storage(tmp_path / "monitor.db")
    await instance.open()
    try:
        yield instance
    finally:
        await instance.close()
```

Run: `python -m pytest tests/test_storage.py -v`

Expected: all tests pass and no SQLite resource warnings are emitted.

- [ ] **Step 6: Commit domain persistence**

```bash
git add 07-web3-bot/DEFI/usd1_monitor/usd1_monitor/models.py 07-web3-bot/DEFI/usd1_monitor/usd1_monitor/storage.py 07-web3-bot/DEFI/usd1_monitor/tests/conftest.py 07-web3-bot/DEFI/usd1_monitor/tests/test_storage.py
git commit -m "功能：持久化监控观测与风险状态"
```

### Task 3: Build the Binance order-book collector and exit-capacity maths

**Files:**
- Create: `usd1_monitor/http.py`
- Create: `usd1_monitor/collectors/__init__.py`
- Create: `usd1_monitor/collectors/market.py`
- Create: `tests/fakes.py`
- Create: `tests/fixtures/binance_depth.json`
- Create: `tests/test_market_collector.py`

- [ ] **Step 1: Write failing order-book calculation tests**

```python
# tests/test_market_collector.py
from decimal import Decimal

from usd1_monitor.collectors.market import analyze_book, simulate_sell


def test_analyze_book_calculates_mid_spread_and_10bps_depth() -> None:
    bids = [["0.9990", "600000"], ["0.9985", "500000"]]
    asks = [["1.0010", "300000"]]
    result = analyze_book(bids, asks)
    assert result.mid == Decimal("1.0000")
    assert result.spread_bps == Decimal("20")
    assert result.bid_depth_10bps == Decimal("599400.0000")


def test_simulate_sell_reports_vwap_terminal_and_fillability() -> None:
    bids = [["0.999", "600000"], ["0.998", "500000"]]
    result = simulate_sell(bids, Decimal("1000000"))
    assert result.fully_fillable is True
    assert result.terminal_price == Decimal("0.998")
    assert result.vwap == Decimal("0.9986")


def test_simulate_sell_marks_truncated_book() -> None:
    result = simulate_sell([["0.999", "100"]], Decimal("1000000"))
    assert result.fully_fillable is False
    assert result.filled_base == Decimal("100")
```

- [ ] **Step 2: Run the calculation tests and verify failure**

Run: `python -m pytest tests/test_market_collector.py -v`

Expected: import fails because `usd1_monitor.collectors.market` does not exist.

- [ ] **Step 3: Implement exact Decimal order-book maths**

```python
# usd1_monitor/collectors/market.py
from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence


@dataclass(frozen=True)
class BookMetrics:
    best_bid: Decimal
    best_ask: Decimal
    mid: Decimal
    spread_bps: Decimal
    bid_depth_10bps: Decimal
    bid_depth_30bps: Decimal


@dataclass(frozen=True)
class SellSimulation:
    requested_base: Decimal
    filled_base: Decimal
    quote_received: Decimal
    vwap: Decimal | None
    terminal_price: Decimal | None
    fully_fillable: bool


def simulate_sell(bids: Sequence[Sequence[str]], size: Decimal) -> SellSimulation:
    remaining = size
    filled = Decimal(0)
    quote = Decimal(0)
    terminal = None
    for raw_price, raw_qty in bids:
        price = Decimal(raw_price)
        qty = Decimal(raw_qty)
        take = min(remaining, qty)
        if take <= 0:
            continue
        filled += take
        quote += take * price
        remaining -= take
        terminal = price
        if remaining == 0:
            break
    return SellSimulation(size, filled, quote, quote / filled if filled else None, terminal, remaining == 0)


def analyze_book(bids: Sequence[Sequence[str]], asks: Sequence[Sequence[str]]) -> BookMetrics:
    if not bids or not asks:
        raise ValueError("order book must contain bids and asks")
    best_bid = Decimal(bids[0][0])
    best_ask = Decimal(asks[0][0])
    mid = (best_bid + best_ask) / 2
    spread_bps = (best_ask - best_bid) / mid * Decimal(10_000)
    floor_10 = mid * Decimal("0.999")
    floor_30 = mid * Decimal("0.997")
    depth_10 = sum(Decimal(p) * Decimal(q) for p, q in bids if Decimal(p) >= floor_10)
    depth_30 = sum(Decimal(p) * Decimal(q) for p, q in bids if Decimal(p) >= floor_30)
    return BookMetrics(best_bid, best_ask, mid, spread_bps, depth_10, depth_30)
```

- [ ] **Step 4: Add the retrying HTTP client and collector adapter**

Implement `AsyncHttpClient.get_json(url, params)` using one shared `aiohttp.ClientSession`, total timeout from `HttpConfig`, `retries + 1` attempts, exponential waits of 1 and 2 seconds, `raise_for_status()`, and a typed `HttpRequestError` containing method, sanitized host/path, attempt count, and original exception. Never include query values from RPC URLs in errors.

Implement `BinanceMarketCollector.collect_symbol(symbol, collected_at)` to call:

```text
GET https://api.binance.com/api/v3/exchangeInfo?symbol={symbol}
GET https://api.binance.com/api/v3/depth?symbol={symbol}&limit=1000
```

Return observations for symbol status, mid price, spread, 10bps/30bps depth, and each configured sell simulation. Store Decimal results as floats only at the Observation boundary and retain `fully_fillable` in metadata.

Create this reusable deterministic HTTP fake for this and later plans:

```python
# tests/fakes.py
class FakeHttp:
    def __init__(self) -> None:
        self.responses: dict[tuple[str, str], list[object]] = {}
        self.calls: list[tuple[str, str, object]] = []

    def queue_json(self, url: str, value: object, method: str = "GET") -> None:
        self.responses.setdefault((method, url), []).append(value)

    def queue_error(self, url: str, error: Exception, method: str = "GET") -> None:
        self.responses.setdefault((method, url), []).append(error)

    async def get_json(self, url: str, params: dict | None = None) -> object:
        self.calls.append(("GET", url, params))
        value = self.responses[("GET", url)].pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    async def post_json(self, url: str, payload: dict) -> object:
        self.calls.append(("POST", url, payload))
        value = self.responses[("POST", url)].pop(0)
        if isinstance(value, Exception):
            raise value
        return value
```

- [ ] **Step 5: Add fixture-driven collector tests**

Use a fake object exposing `async get_json(url, params)`; do not mock aiohttp internals. Assert exact endpoint parameters, `TRADING` status parsing, emitted metric names, and that malformed/empty books raise `MarketDataError` rather than emit zeros.

Run: `python -m pytest tests/test_market_collector.py -v`

Expected: all market tests pass.

- [ ] **Step 6: Commit market collection**

```bash
git add 07-web3-bot/DEFI/usd1_monitor/usd1_monitor/http.py 07-web3-bot/DEFI/usd1_monitor/usd1_monitor/collectors 07-web3-bot/DEFI/usd1_monitor/tests/fakes.py 07-web3-bot/DEFI/usd1_monitor/tests/fixtures/binance_depth.json 07-web3-bot/DEFI/usd1_monitor/tests/test_market_collector.py
git commit -m "功能：采集 USD1 盘口与退出容量"
```

### Task 4: Implement market risk transitions and durable de-duplication

**Files:**
- Create: `usd1_monitor/engine/__init__.py`
- Create: `usd1_monitor/engine/market_rules.py`
- Create: `usd1_monitor/engine/state.py`
- Create: `tests/test_market_rules.py`
- Modify: `usd1_monitor/storage.py`

- [ ] **Step 1: Write failing state-transition tests with an injected clock**

```python
# tests/test_market_rules.py
from datetime import UTC, datetime, timedelta

from usd1_monitor.engine.market_rules import MarketSnapshot, evaluate_market
from usd1_monitor.models import RiskLevel


START = datetime(2026, 9, 7, 4, 0, tzinfo=UTC)


def snapshot(at: datetime, price: float, terminal: float = 0.998, fillable: bool = True) -> MarketSnapshot:
    return MarketSnapshot(
        observed_at=at,
        mids={"USD1USDT": price, "USD1USDC": price},
        trading={"USD1USDT": True, "USD1USDC": True},
        one_million_terminal={"USD1USDT": terminal, "USD1USDC": terminal},
        one_million_fillable={"USD1USDT": fillable, "USD1USDC": fillable},
    )


def test_price_becomes_yellow_only_after_fifteen_valid_minutes() -> None:
    history = [snapshot(START, 0.996), snapshot(START + timedelta(minutes=14), 0.996)]
    assert evaluate_market(history).price_level is RiskLevel.GREEN
    history.append(snapshot(START + timedelta(minutes=15), 0.996))
    assert evaluate_market(history).price_level is RiskLevel.YELLOW


def test_price_becomes_red_after_five_minutes_below_0995() -> None:
    history = [snapshot(START, 0.994), snapshot(START + timedelta(minutes=5), 0.994)]
    assert evaluate_market(history).price_level is RiskLevel.RED


def test_missing_observation_does_not_count_toward_duration() -> None:
    history = [snapshot(START, 0.996), snapshot(START + timedelta(minutes=20), 0.996)]
    assert evaluate_market(history, max_gap_seconds=90).price_level is RiskLevel.GREEN


def test_two_unfillable_books_are_red() -> None:
    history = [snapshot(START, 1.0, fillable=False), snapshot(START + timedelta(minutes=1), 1.0, fillable=False)]
    assert evaluate_market(history).liquidity_level is RiskLevel.RED
```

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest tests/test_market_rules.py -v`

Expected: import fails because market rules do not exist.

- [ ] **Step 3: Implement pure market evaluation**

Define immutable `MarketSnapshot` and `MarketEvaluation(price_level, liquidity_level, trading_level, evidence)`. Implement a helper that only regards observations as continuous when adjacent timestamps differ by at most `max_gap_seconds`; evaluate exact thresholds from the approved spec. Evaluate trading status and book fillability as two consecutive valid observations. Do not access SQLite, environment variables, or the network from this module.

The public signature is:

```python
def evaluate_market(
    history: list[MarketSnapshot],
    *,
    yellow_price: float = 0.997,
    yellow_seconds: int = 900,
    red_price: float = 0.995,
    red_seconds: int = 300,
    recovery_price: float = 0.998,
    recovery_seconds: int = 900,
    max_gap_seconds: int = 90,
) -> MarketEvaluation:
```

- [ ] **Step 4: Implement durable transition recording**

Add `RiskTransition(rule_id, previous, current, changed_at, first_triggered_at, evidence)` to `models.py`. Implement `StateEngine.apply(evaluations, now)` so it reads the prior row, writes only changed states, preserves `first_triggered_at` while a risk remains active, and returns transitions only for actual level changes. Wrap state update and pending alert insertion in one SQLite transaction.

- [ ] **Step 5: Test recovery and reopen behavior**

Add tests asserting `RED → YELLOW`, `YELLOW → GREEN`, no duplicate transition at the same level, and reopening SQLite does not replay an alert. Run:

`python -m pytest tests/test_market_rules.py tests/test_storage.py -v`

Expected: all tests pass.

- [ ] **Step 6: Commit market rules**

```bash
git add 07-web3-bot/DEFI/usd1_monitor/usd1_monitor/engine 07-web3-bot/DEFI/usd1_monitor/usd1_monitor/models.py 07-web3-bot/DEFI/usd1_monitor/usd1_monitor/storage.py 07-web3-bot/DEFI/usd1_monitor/tests/test_market_rules.py 07-web3-bot/DEFI/usd1_monitor/tests/test_storage.py
git commit -m "功能：实现市场风险状态机"
```

### Task 5: Add collector health and WeChat delivery

**Files:**
- Create: `usd1_monitor/engine/health.py`
- Create: `usd1_monitor/notifications/__init__.py`
- Create: `usd1_monitor/notifications/wechat.py`
- Create: `tests/test_health.py`
- Create: `tests/test_wechat.py`
- Modify: `usd1_monitor/storage.py`

- [ ] **Step 1: Write failing health tests**

```python
# tests/test_health.py
from datetime import UTC, datetime, timedelta

from usd1_monitor.engine.health import CollectorHealth, evaluate_health
from usd1_monitor.models import RiskLevel


NOW = datetime(2026, 9, 7, 4, 30, tzinfo=UTC)


def test_three_failures_are_yellow() -> None:
    health = CollectorHealth("binance", 3, NOW - timedelta(minutes=2), "timeout")
    assert evaluate_health(health, NOW, critical=True) is RiskLevel.YELLOW


def test_critical_source_stale_for_fifteen_minutes_is_red() -> None:
    health = CollectorHealth("por", 1, NOW - timedelta(minutes=16), "rpc timeout")
    assert evaluate_health(health, NOW, critical=True) is RiskLevel.RED
```

- [ ] **Step 2: Write failing WeChat business-error test**

```python
# tests/test_wechat.py
import pytest

from usd1_monitor.notifications.wechat import WeChatDeliveryError, WeChatNotifier


class FakeHttp:
    async def post_json(self, url: str, payload: dict) -> dict:
        return {"errcode": 93000, "errmsg": "invalid webhook"}


@pytest.mark.asyncio
async def test_http_200_business_error_is_delivery_failure() -> None:
    notifier = WeChatNotifier("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=redacted", FakeHttp())
    with pytest.raises(WeChatDeliveryError, match="93000"):
        await notifier.send_text("risk")
```

- [ ] **Step 3: Run both files and verify failure**

Run: `python -m pytest tests/test_health.py tests/test_wechat.py -v`

Expected: imports fail because health and notification modules do not exist.

- [ ] **Step 4: Implement health rules and notification delivery**

Implement `CollectorHealth` and `evaluate_health()` exactly as tested: three consecutive failures are yellow; a critical source with no success for 15 minutes is red; a successful run resets the count and permits a recovery transition.

Implement `WeChatNotifier.send_text()` with payload:

```python
payload = {"msgtype": "text", "text": {"content": content}}
```

Treat nonzero/missing `errcode` as failure. Save every delivery attempt to `alert_deliveries`; only set `delivered_at` after a zero `errcode`. Retry failed pending deliveries at most twice and keep the last error summary. Sanitize the webhook key from all logs.

- [ ] **Step 5: Add alert formatting tests**

Assert a transition message contains level, rule ID, current value, threshold, first-triggered time, data time, and source link; assert a recovery message names the previous level; assert two transitions with the same `cause_id` are rendered once with two evidence lines.

Run: `python -m pytest tests/test_health.py tests/test_wechat.py -v`

Expected: all tests pass.

- [ ] **Step 6: Commit health and notification handling**

```bash
git add 07-web3-bot/DEFI/usd1_monitor/usd1_monitor/engine/health.py 07-web3-bot/DEFI/usd1_monitor/usd1_monitor/notifications 07-web3-bot/DEFI/usd1_monitor/usd1_monitor/storage.py 07-web3-bot/DEFI/usd1_monitor/tests/test_health.py 07-web3-bot/DEFI/usd1_monitor/tests/test_wechat.py
git commit -m "功能：增加监控盲区与企业微信通知"
```

### Task 6: Wire the scheduler and CLI into a working market monitor

**Files:**
- Create: `usd1_monitor/scheduler.py`
- Create: `usd1_monitor/cli.py`
- Create: `usd1_monitor/__main__.py`
- Create: `usd1_monitor/logging_config.py`
- Create: `tests/test_cli.py`
- Create: `tests/test_market_integration.py`
- Modify: `tests/conftest.py`
- Modify: `config.example.yaml`

- [ ] **Step 1: Write a failing end-to-end fake-data test**

```python
# tests/test_market_integration.py
from datetime import UTC, datetime, timedelta

import pytest

from usd1_monitor.scheduler import MarketMonitor


@pytest.mark.asyncio
async def test_market_monitor_persists_one_yellow_transition(fake_market_collector, storage, fake_notifier) -> None:
    fake_market_collector.queue_price(0.996, datetime(2026, 9, 7, 4, 0, tzinfo=UTC))
    fake_market_collector.queue_price(0.996, datetime(2026, 9, 7, 4, 15, tzinfo=UTC))
    monitor = MarketMonitor(fake_market_collector, storage, fake_notifier)
    await monitor.check_once()
    await monitor.check_once()
    assert len(fake_notifier.messages) == 1
    assert "YELLOW" in fake_notifier.messages[0]
```

Add `FakeMarketCollector` and `FakeNotifier` fixtures to `tests/conftest.py`. The collector must expose `queue_price(price, observed_at)` and `async collect_symbol(symbol, collected_at)`; each queued price is returned for both configured symbols with a trading status, fillable one-million simulation, and the queued observation time. The notifier exposes `messages: list[str]` and `async send_text(content)` that only appends to that list.

- [ ] **Step 2: Run the integration test and verify failure**

Run: `python -m pytest tests/test_market_integration.py -v`

Expected: import fails because `usd1_monitor.scheduler` does not exist.

- [ ] **Step 3: Implement scheduler ownership and graceful shutdown**

`MarketMonitor.check_once()` must collect both symbols, persist observations, build a complete snapshot only when both sources are valid, evaluate rules, commit transitions, and then deliver pending alerts. On collector failure it updates collector health and continues. `run()` repeats at the configured interval using monotonic scheduling and responds to SIGTERM/SIGINT by finishing the current SQLite transaction and closing HTTP/SQLite resources.

Implement `configure_logging(path, max_bytes=10_000_000, backup_count=5)` with `logging.handlers.RotatingFileHandler` plus a stream handler. Add log path, max bytes, and backup count to strict configuration. Secrets and RPC query strings must pass through the HTTP sanitizer before logging.

When `run()` starts with a configured notifier, send one startup message containing version `0.1.0`, enabled collectors, and the complete `NOT_MONITORED` list. Add a test asserting exactly one startup message per process invocation and no startup message from `check`.

- [ ] **Step 4: Implement the three CLI commands**

Use `argparse` with this contract:

```text
python -m usd1_monitor --config config.yaml check
python -m usd1_monitor --config config.yaml run
python -m usd1_monitor --config config.yaml status
```

`check` exits 0 only when every enabled collector returns valid data and never sends a webhook. `run` logs that notifications are disabled when no webhook is configured. Define a module-level immutable `NOT_MONITORED` tuple containing `private_exchange_account`, `active_conversion_probe`, `tron_solana_aptos_bridges`, `binance_wallet_concentration`, `social_media_sentiment`, `defi_liquidations`, and `web_dashboard`. `status` prints overall risk, market sub-rules, collector last-success timestamps, and every item in that tuple.

- [ ] **Step 5: Test exit codes and no-alert check mode**

Add CLI tests with temporary config/SQLite and injected fake monitor factories. Assert invalid configuration exits 2, failed check exits 1, successful check exits 0, and check mode never calls the notifier.

Run: `python -m pytest tests/test_cli.py tests/test_market_integration.py -v`

Expected: all tests pass.

- [ ] **Step 6: Run the phase test suite**

Run: `python -m pytest -v`

Expected: all phase-one tests pass with no real network or webhook access.

- [ ] **Step 7: Commit the runnable market monitor**

```bash
git add 07-web3-bot/DEFI/usd1_monitor/usd1_monitor/scheduler.py 07-web3-bot/DEFI/usd1_monitor/usd1_monitor/cli.py 07-web3-bot/DEFI/usd1_monitor/usd1_monitor/__main__.py 07-web3-bot/DEFI/usd1_monitor/usd1_monitor/logging_config.py 07-web3-bot/DEFI/usd1_monitor/tests/conftest.py 07-web3-bot/DEFI/usd1_monitor/tests/test_cli.py 07-web3-bot/DEFI/usd1_monitor/tests/test_market_integration.py 07-web3-bot/DEFI/usd1_monitor/config.example.yaml
git commit -m "功能：交付 USD1 市场监控命令行程序"
```
