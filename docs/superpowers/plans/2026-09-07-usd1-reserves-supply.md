# USD1 Reserves and Supply Monitoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the monitor with verified PoR Oracle decoding, ETH/BSC supply reads, disambiguated DefiLlama global supply, estimated collateralization, and reserve/supply risk transitions.

**Architecture:** The reserve collector performs read-only Ethereum `eth_call` requests and rejects internally inconsistent bundles. The supply collector reads native-chain total supplies and obtains a separately labeled global estimate from DefiLlama. Pure rules keep factual, estimated, and health states distinct.

**Tech Stack:** Python 3.11+, existing phase-one/two stack, eth-abi, eth-utils, pytest

---

Complete the core-market and EVM plans first. Run commands from `07-web3-bot/DEFI/usd1_monitor`.

### Task 1: Decode and validate the USD1 PoR Oracle bundle

**Files:**
- Modify: `usd1_monitor/config.py`
- Modify: `config.example.yaml`
- Create: `usd1_monitor/collectors/reserves.py`
- Create: `tests/test_reserves_collector.py`

- [ ] **Step 1: Write failing bundle tests**

```python
# tests/test_reserves_collector.py
from datetime import UTC, datetime

import pytest
from eth_abi import encode

from usd1_monitor.collectors.reserves import PorDataError, decode_por_calls


def rpc_bytes(value: bytes) -> str:
    return "0x" + encode(["bytes"], [value]).hex()


def test_decode_por_bundle_checks_timestamp_and_decimals() -> None:
    bundle = encode(["uint256", "uint256"], [1_788_753_600, 4_250_000_000 * 10**18])
    result = decode_por_calls(
        latest_bundle=rpc_bytes(bundle),
        latest_timestamp="0x" + encode(["uint256"], [1_788_753_600]).hex(),
        bundle_decimals="0x" + encode(["uint8[]"], [[18]]).hex(),
        collected_at=datetime(2026, 9, 7, 4, 0, tzinfo=UTC),
    )
    assert result.reserves == 4_250_000_000
    assert result.oracle_timestamp == 1_788_753_600


def test_decode_por_bundle_rejects_timestamp_mismatch() -> None:
    bundle = encode(["uint256", "uint256"], [100, 4_250_000_000 * 10**18])
    with pytest.raises(PorDataError, match="timestamp mismatch"):
        decode_por_calls(
            latest_bundle=rpc_bytes(bundle),
            latest_timestamp="0x" + encode(["uint256"], [101]).hex(),
            bundle_decimals="0x" + encode(["uint8[]"], [[18]]).hex(),
            collected_at=datetime(2026, 9, 7, 4, 0, tzinfo=UTC),
        )
```

- [ ] **Step 2: Run and verify failure**

Run: `python -m pytest tests/test_reserves_collector.py -v`

Expected: import fails because the reserve collector does not exist.

- [ ] **Step 3: Add PoR configuration**

Add `PorConfig` with address `0x691b74146cdba162449012aa32d3cbf5df77d4c4`, interval 300 seconds, yellow staleness 3600 seconds, red staleness 7200 seconds, relative-change threshold 0.005, and recovery read count 2. Validate the address and strictly increasing staleness thresholds.

- [ ] **Step 4: Implement call encoding and bundle decoding**

```python
# core interfaces in usd1_monitor/collectors/reserves.py
from dataclasses import dataclass
from datetime import datetime

from eth_abi import decode
from eth_utils import keccak


class PorDataError(ValueError):
    pass


@dataclass(frozen=True)
class PorSnapshot:
    reserves: float
    oracle_timestamp: int
    observed_at: datetime
    collected_at: datetime


def selector(signature: str) -> str:
    return "0x" + keccak(text=signature)[:4].hex()


def decode_por_calls(*, latest_bundle: str, latest_timestamp: str, bundle_decimals: str, collected_at: datetime) -> PorSnapshot:
    bundle = decode(["bytes"], bytes.fromhex(latest_bundle[2:]))[0]
    bundle_timestamp, raw_reserves = decode(["uint256", "uint256"], bundle)
    timestamp = decode(["uint256"], bytes.fromhex(latest_timestamp[2:]))[0]
    decimals = decode(["uint8[]"], bytes.fromhex(bundle_decimals[2:]))[0]
    if bundle_timestamp != timestamp:
        raise PorDataError("timestamp mismatch between latestBundle and latestBundleTimestamp")
    if len(decimals) < 1:
        raise PorDataError("bundleDecimals must contain the reserves entry")
    reserves = raw_reserves / (10 ** decimals[0])
    return PorSnapshot(float(reserves), int(timestamp), datetime.fromtimestamp(timestamp, tz=collected_at.tzinfo), collected_at)
```

`PorCollector.collect()` performs three `eth_call`s at the same confirmed block using selectors for `latestBundle()`, `latestBundleTimestamp()`, and `bundleDecimals()`. It emits reserves and oracle-age observations only after all validations pass.

- [ ] **Step 5: Test RPC block consistency and malformed bundles**

Assert all three calls use the same block tag; truncated hex, zero timestamp, future timestamp beyond five minutes, and missing decimals raise `PorDataError` and emit no observation.

Run: `python -m pytest tests/test_reserves_collector.py -v`

Expected: all tests pass.

- [ ] **Step 6: Commit PoR collection**

```bash
git add 07-web3-bot/DEFI/usd1_monitor/usd1_monitor/config.py 07-web3-bot/DEFI/usd1_monitor/config.example.yaml 07-web3-bot/DEFI/usd1_monitor/usd1_monitor/collectors/reserves.py 07-web3-bot/DEFI/usd1_monitor/tests/test_reserves_collector.py
git commit -m "功能：读取并校验 USD1 储备预言机"
```

### Task 2: Read native supply and disambiguate DefiLlama USD1

**Files:**
- Create: `usd1_monitor/collectors/supply.py`
- Create: `tests/fixtures/defillama_stablecoins.json`
- Create: `tests/test_supply_collector.py`
- Modify: `usd1_monitor/config.py`
- Modify: `config.example.yaml`

- [ ] **Step 1: Write failing identity and estimated-coverage tests**

```python
# tests/test_supply_collector.py
import pytest

from usd1_monitor.collectors.supply import SupplyDataError, choose_defillama_asset, estimated_coverage


def test_choose_world_liberty_usd1_and_exclude_unitas() -> None:
    assets = [
        {"id": "101", "symbol": "USD1", "name": "Unitas"},
        {"id": "202", "symbol": "USD1", "name": "World Liberty Financial USD"},
    ]
    assert choose_defillama_asset(assets)["id"] == "202"


def test_duplicate_world_liberty_match_is_rejected() -> None:
    assets = [
        {"id": "202", "symbol": "USD1", "name": "World Liberty Financial USD"},
        {"id": "203", "symbol": "USD1", "name": "World Liberty Financial USD"},
    ]
    with pytest.raises(SupplyDataError, match="exactly one"):
        choose_defillama_asset(assets)


def test_estimated_coverage_is_labeled_estimate() -> None:
    result = estimated_coverage(reserves=4_250_000_000, global_supply=4_200_000_000)
    assert result.ratio_percent == pytest.approx(101.190476)
    assert result.quality == "ESTIMATED"
```

- [ ] **Step 2: Run and verify failure**

Run: `python -m pytest tests/test_supply_collector.py -v`

Expected: import fails because the supply collector does not exist.

- [ ] **Step 3: Implement strict asset selection**

```python
# public helpers in usd1_monitor/collectors/supply.py
from dataclasses import dataclass


class SupplyDataError(ValueError):
    pass


@dataclass(frozen=True)
class EstimatedCoverage:
    ratio_percent: float
    quality: str = "ESTIMATED"


def choose_defillama_asset(assets: list[dict]) -> dict:
    matches = [a for a in assets if a.get("symbol") == "USD1" and a.get("name") == "World Liberty Financial USD"]
    if len(matches) != 1:
        raise SupplyDataError(f"expected exactly one World Liberty Financial USD asset, found {len(matches)}")
    return matches[0]


def estimated_coverage(*, reserves: float, global_supply: float) -> EstimatedCoverage:
    if reserves <= 0 or global_supply <= 0:
        raise SupplyDataError("reserves and global supply must be positive")
    return EstimatedCoverage(reserves / global_supply * 100)
```

- [ ] **Step 4: Implement on-chain and DefiLlama reads**

For each native chain call `decimals()` and `totalSupply()` at the chain's confirmed block, decode ABI uint values, reject decimals outside 0–36, and emit `supply.native` with quality `FACT`. Fetch `https://stablecoins.llama.fi/stablecoins?includePrices=true`, select the exact asset, and read its current `circulating.peggedUSD`; emit `supply.global` with quality `ESTIMATED_SOURCE`. Missing or nonpositive values raise `SupplyDataError`.

Add a one-hour interval and exact DefiLlama expected symbol/name to config.

- [ ] **Step 5: Test call decoding and malformed API shapes**

Add tests for 18-decimal totalSupply, a decimals call revert, missing `circulating.peggedUSD`, zero supply, and Unitas-only input. Assert no fallback picks the first symbol match.

Run: `python -m pytest tests/test_supply_collector.py -v`

Expected: all tests pass.

- [ ] **Step 6: Commit supply collection**

```bash
git add 07-web3-bot/DEFI/usd1_monitor/usd1_monitor/collectors/supply.py 07-web3-bot/DEFI/usd1_monitor/usd1_monitor/config.py 07-web3-bot/DEFI/usd1_monitor/config.example.yaml 07-web3-bot/DEFI/usd1_monitor/tests/fixtures/defillama_stablecoins.json 07-web3-bot/DEFI/usd1_monitor/tests/test_supply_collector.py
git commit -m "功能：采集 USD1 原生与全链供应量"
```

### Task 3: Implement reserve and supply risk rules

**Files:**
- Create: `usd1_monitor/engine/reserve_rules.py`
- Create: `usd1_monitor/engine/supply_rules.py`
- Create: `tests/test_reserve_rules.py`
- Create: `tests/test_supply_rules.py`

- [ ] **Step 1: Write failing PoR staleness tests**

```python
# tests/test_reserve_rules.py
from datetime import UTC, datetime, timedelta

from usd1_monitor.engine.reserve_rules import evaluate_por_age
from usd1_monitor.models import RiskLevel


NOW = datetime(2026, 9, 7, 4, 0, tzinfo=UTC)


def test_por_age_boundaries() -> None:
    assert evaluate_por_age(NOW - timedelta(seconds=3600), NOW) is RiskLevel.GREEN
    assert evaluate_por_age(NOW - timedelta(seconds=3601), NOW) is RiskLevel.YELLOW
    assert evaluate_por_age(NOW - timedelta(seconds=7201), NOW) is RiskLevel.RED
```

- [ ] **Step 2: Write failing estimated-coverage composite tests**

```python
# tests/test_supply_rules.py
from usd1_monitor.engine.supply_rules import SupplyRiskInput, evaluate_supply
from usd1_monitor.models import RiskLevel


def test_estimated_ratio_below_100_is_only_yellow_by_itself() -> None:
    result = evaluate_supply(SupplyRiskInput([99.8, 99.7], native_drop_24h=0.0, market_level=RiskLevel.GREEN))
    assert result.coverage_level is RiskLevel.YELLOW


def test_ratio_below_99_and_market_warning_is_red() -> None:
    result = evaluate_supply(SupplyRiskInput([98.8, 98.7], native_drop_24h=0.0, market_level=RiskLevel.YELLOW))
    assert result.coverage_level is RiskLevel.RED


def test_two_percent_native_drop_and_market_warning_is_red() -> None:
    result = evaluate_supply(SupplyRiskInput([101.0, 101.0], native_drop_24h=0.021, market_level=RiskLevel.YELLOW))
    assert result.native_supply_level is RiskLevel.RED
```

- [ ] **Step 3: Run and verify failure**

Run: `python -m pytest tests/test_reserve_rules.py tests/test_supply_rules.py -v`

Expected: imports fail because reserve/supply rules do not exist.

- [ ] **Step 4: Implement pure rules**

`evaluate_por_age()` uses strict `>` comparisons at 3600 and 7200 seconds. Reserve relative change `abs(current - previous) / previous > 0.005` is yellow. Recovery requires two consecutive valid Oracle reads within one hour.

`evaluate_supply()` makes two consecutive hourly estimated ratios below 100 yellow; two below 99 plus market yellow/red is red; an absolute estimated-ratio change above 0.5 percentage points is yellow. ETH+BSC 24-hour net drop of at least 2% is yellow alone and red with market yellow/red. Missing points produce unknown, never zero.

- [ ] **Step 5: Add boundary and recovery tests**

Assert exactly 100%, exactly 99%, exactly 0.5 percentage points, exactly 2%, missing history, stale market input, and two-reading recovery. Use explicit expected `GREEN/YELLOW/RED` values for each case.

Run: `python -m pytest tests/test_reserve_rules.py tests/test_supply_rules.py -v`

Expected: all tests pass.

- [ ] **Step 6: Commit reserve and supply rules**

```bash
git add 07-web3-bot/DEFI/usd1_monitor/usd1_monitor/engine/reserve_rules.py 07-web3-bot/DEFI/usd1_monitor/usd1_monitor/engine/supply_rules.py 07-web3-bot/DEFI/usd1_monitor/tests/test_reserve_rules.py 07-web3-bot/DEFI/usd1_monitor/tests/test_supply_rules.py
git commit -m "功能：判断 USD1 储备与供应风险"
```

### Task 4: Integrate reserve and supply schedules

**Files:**
- Modify: `usd1_monitor/scheduler.py`
- Modify: `usd1_monitor/cli.py`
- Modify: `tests/fakes.py`
- Create: `tests/test_reserve_supply_integration.py`

- [ ] **Step 1: Write a failing isolated-collector integration test**

```python
# tests/test_reserve_supply_integration.py
import pytest

from tests.fakes import FakeNotifier, FakePorCollector, FakeSupplyCollector
from usd1_monitor.scheduler import ReserveSupplyMonitor

@pytest.mark.asyncio
async def test_por_failure_does_not_block_supply(storage) -> None:
    por = FakePorCollector()
    supply = FakeSupplyCollector()
    por.queue_error(TimeoutError("oracle rpc unavailable"))
    supply.queue_global_supply(4_200_000_000)
    monitor = ReserveSupplyMonitor(por, supply, storage, FakeNotifier())
    await monitor.check_once()
    rows = await storage.latest_observations("supply.global", limit=1)
    assert rows[0].value == 4_200_000_000
```

- [ ] **Step 2: Run and verify failure**

Run: `python -m pytest tests/test_reserve_supply_integration.py -v`

Expected: the monitor lacks reserve/supply scheduling.

- [ ] **Step 3: Add independent schedules and data joining**

Schedule PoR every 5 minutes and supply every hour with separate health keys. Estimated coverage is emitted only when both inputs are positive and no older than 75 minutes. Join the current market level for composite rules; if market data is stale, the composite remains unknown and cannot become red solely from stale state.

Append deterministic fakes exposing `queue_error()`/`collect()` for PoR, `queue_global_supply()`/`collect()` for supply, and `messages`/`send_text()` for notifications. Implement `ReserveSupplyMonitor(por_collector, supply_collector, storage, notifier).check_once()` as the integration seam tested above; the top-level scheduler calls this seam at the two configured intervals.

- [ ] **Step 4: Extend CLI output**

`check` prints Oracle data time/age, reserves, ETH supply, BSC supply, DefiLlama global supply, estimated ratio with `ESTIMATED`, and collector status. `status` prints reserve/supply sub-rules and labels full multi-chain reconciliation `NOT_MONITORED`.

- [ ] **Step 5: Run all tests and commit**

Run: `python -m pytest -v`

Expected: market, EVM, reserve, and supply suites pass without live network calls.

```bash
git add 07-web3-bot/DEFI/usd1_monitor/usd1_monitor/scheduler.py 07-web3-bot/DEFI/usd1_monitor/usd1_monitor/cli.py 07-web3-bot/DEFI/usd1_monitor/tests/fakes.py 07-web3-bot/DEFI/usd1_monitor/tests/test_reserve_supply_integration.py
git commit -m "功能：接入 USD1 储备与供应监控"
```
