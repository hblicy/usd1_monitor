# USD1 Full Multichain Supply Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 每小时核对 USD1 在 6 条原生链、5 条桥接链和 5 个 CCIP 桥池上的供应量，并用完整原生供应量计算储备覆盖率和桥接差额风险。

**Architecture:** 新增静态资产清单、EVM 与非 EVM 协议读取器及一个有界并发的多链编排器，所有读取器统一产生现有 `Observation`/`SupplySnapshot`。`ReserveSupplyMonitor` 只在同一轮 16 个必需组件齐全时原子写入聚合指标和风险状态；DefiLlama 保留为辅助读数，健康通知通过单一聚合规则降噪。

**Tech Stack:** Python 3.12、asyncio、aiohttp、Pydantic、aiosqlite、eth-abi、pytest、pytest-asyncio

---

## 文件结构

- Create: `usd1_monitor/supply_assets.py` — 固化官方链、资产、桥池、精度和浏览器地址。
- Create: `usd1_monitor/collectors/multichain_evm.py` — 读取 EVM `totalSupply()` 和 `balanceOf()`，同链复用区块号。
- Create: `usd1_monitor/collectors/non_evm_supply.py` — 读取 Tron、Solana、Aptos 供应量和桥池余额。
- Create: `usd1_monitor/collectors/multichain_supply.py` — 有界并发、完整性检查和四个聚合指标。
- Modify: `usd1_monitor/config.py` — 新增端点配置和环境变量覆盖。
- Modify: `usd1_monitor/engine/supply_rules.py` — 新增桥接差额确认与恢复状态机。
- Modify: `usd1_monitor/scheduler.py` — 原子持久化、覆盖率、24 小时下降及健康状态接线。
- Modify: `usd1_monitor/cli.py` — 构建各链客户端并扩展 `check/status` 输出。
- Modify: `usd1_monitor/notifications/wechat.py` — 新风险和合并健康消息的简明中文格式。
- Modify: `config.example.yaml`, `deploy/config.production.example.yaml`, `.env.example`, `README.md` — 部署配置和能力边界。
- Create/Modify tests listed in each task — 先失败、后最小实现、再回归。

### Task 1: 固化官方资产清单并扩展端点配置

**Files:**
- Create: `usd1_monitor/supply_assets.py`
- Create: `tests/test_supply_assets.py`
- Modify: `usd1_monitor/config.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: 写入官方资产清单和旧配置兼容测试**

```python
# tests/test_supply_assets.py
from usd1_monitor.supply_assets import (
    APTOS_METADATA,
    BRIDGED_EVM_SPECS,
    EVM_CHAIN_IDS,
    NATIVE_EVM_SPECS,
    SOLANA_MINT,
    TRON_TOKEN,
)


def test_official_multichain_asset_inventory_is_exact() -> None:
    assert {item.scope for item in NATIVE_EVM_SPECS} == {
        "ethereum", "bsc", "tempo"
    }
    assert {item.scope for item in BRIDGED_EVM_SPECS} == {
        "plume", "ab", "monad", "mantle", "morph"
    }
    assert EVM_CHAIN_IDS == {
        "ethereum": 1,
        "bsc": 56,
        "tempo": 4217,
        "plume": 98866,
        "ab": 36888,
        "monad": 143,
        "mantle": 5000,
        "morph": 2818,
    }
    assert TRON_TOKEN == "TPFqcBAaaUMCSVRCqPaQ9QnzKhmuoLR6Rc"
    assert SOLANA_MINT == "USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB"
    assert APTOS_METADATA.endswith("e855437d2")
```

```python
# tests/test_config.py
def test_old_config_gets_multichain_public_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("database_path: monitor.db\n", encoding="utf-8")

    config = load_config(path, environ={})

    assert config.supply.multichain.tempo_rpc_urls == [
        "https://rpc.presto.tempo.xyz"
    ]
    assert config.supply.multichain.aptos_indexer_urls == [
        "https://api.mainnet.aptoslabs.com/v1/graphql"
    ]


def test_multichain_rpc_environment_overrides_are_comma_separated(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("database_path: monitor.db\n", encoding="utf-8")

    config = load_config(
        path,
        environ={
            "TEMPO_RPC_URLS": "https://tempo-one.example,https://tempo-two.example",
            "SOLANA_RPC_URLS": "https://solana.example",
        },
    )

    assert config.supply.multichain.tempo_rpc_urls == [
        "https://tempo-one.example",
        "https://tempo-two.example",
    ]
    assert config.supply.multichain.solana_rpc_urls == [
        "https://solana.example"
    ]
```

- [ ] **Step 2: 运行定向测试并确认模块和配置字段尚不存在**

Run: `python -m pytest tests/test_supply_assets.py tests/test_config.py -q`

Expected: FAIL，导入 `usd1_monitor.supply_assets` 或访问 `supply.multichain` 失败。

- [ ] **Step 3: 创建不可变资产清单**

```python
# usd1_monitor/supply_assets.py
from dataclasses import dataclass


USD1_EVM = "0x8d0D000Ee44948FC98c9B98A4FA4921476f08B0d"
USD1_BRIDGED = "0x111111d2bf19e43C34263401e0CAd979eD1cdb61"
USD1_TEMPO = "0x20C000000000000000000000111111111E910F0f"
TRON_TOKEN = "TPFqcBAaaUMCSVRCqPaQ9QnzKhmuoLR6Rc"
SOLANA_MINT = "USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB"
SOLANA_POOL_TOKEN_ACCOUNT = "8c2WaLy3aW9rnFaq8cCZUYnQNSVa9oX2eUun4buZqhmf"
APTOS_METADATA = (
    "0x05fabd1b12e39967a3c24e91b7b8f67719a6dacee74f3c8b9fb7d93e855437d2"
)
APTOS_POOL = (
    "0x1eb155d08acc900954b6ccee01659b390399ae81ad4c582b73d41374c475caf6"
)

EVM_CHAIN_IDS = {
    "ethereum": 1,
    "bsc": 56,
    "tempo": 4217,
    "plume": 98866,
    "ab": 36888,
    "monad": 143,
    "mantle": 5000,
    "morph": 2818,
}


@dataclass(frozen=True)
class EvmSupplySpec:
    component_id: str
    metric: str
    scope: str
    token_address: str
    decimals: int
    explorer_url: str
    holder_address: str | None = None


NATIVE_ETHEREUM = EvmSupplySpec("native_ethereum", "supply.native", "ethereum", USD1_EVM, 18, "https://etherscan.io")
NATIVE_BSC = EvmSupplySpec("native_bsc", "supply.native", "bsc", USD1_EVM, 18, "https://bscscan.com")
NATIVE_TEMPO = EvmSupplySpec("native_tempo", "supply.native", "tempo", USD1_TEMPO, 6, "https://explore.tempo.xyz")
NATIVE_EVM_SPECS = (NATIVE_ETHEREUM, NATIVE_BSC, NATIVE_TEMPO)
BRIDGED_EVM_SPECS = tuple(
    EvmSupplySpec(f"bridged_{scope}", "supply.bridged", scope, USD1_BRIDGED, decimals, explorer)
    for scope, decimals, explorer in (
        ("plume", 18, "https://explorer.plume.org"),
        ("ab", 18, "https://explorer.core.ab.org"),
        ("monad", 6, "https://monadscan.com"),
        ("mantle", 18, "https://mantlescan.xyz"),
        ("morph", 18, "https://explorer.morphl2.io"),
    )
)
LOCKED_ETHEREUM = EvmSupplySpec("locked_ethereum", "bridge.locked", "ethereum", USD1_EVM, 18, "https://etherscan.io", "0x36a72eD0096B414521C45E3ddC9ed657d1D9c141")
LOCKED_BSC = EvmSupplySpec("locked_bsc", "bridge.locked", "bsc", USD1_EVM, 18, "https://bscscan.com", "0xCe3f7378aE409e1CE0dD6fFA70ab683326b73f04")
LOCKED_TEMPO = EvmSupplySpec("locked_tempo", "bridge.locked", "tempo", USD1_TEMPO, 6, "https://explore.tempo.xyz", "0x891F30e80B0809800BbaB14633F9eCe8Fc210024")
LOCKED_EVM_SPECS = (LOCKED_ETHEREUM, LOCKED_BSC, LOCKED_TEMPO)
```

The implementation must format these constructors across lines while preserving the exact values above.

- [ ] **Step 4: 增加严格 HTTPS 端点模型和九个环境变量覆盖**

```python
# usd1_monitor/config.py
class MultichainSupplyConfig(StrictModel):
    tron_rpc_urls: list[str] = Field(default_factory=lambda: [
        "https://api.trongrid.io", "https://api.tronstack.io"
    ])
    solana_rpc_urls: list[str] = Field(default_factory=lambda: [
        "https://solana-rpc.publicnode.com",
        "https://api.mainnet-beta.solana.com",
    ])
    aptos_indexer_urls: list[str] = Field(default_factory=lambda: [
        "https://api.mainnet.aptoslabs.com/v1/graphql"
    ])
    tempo_rpc_urls: list[str] = Field(default_factory=lambda: ["https://rpc.presto.tempo.xyz"])
    plume_rpc_urls: list[str] = Field(default_factory=lambda: ["https://rpc.plume.org"])
    ab_rpc_urls: list[str] = Field(default_factory=lambda: ["https://rpc.core.ab.org"])
    monad_rpc_urls: list[str] = Field(default_factory=lambda: ["https://rpc.monad.xyz"])
    mantle_rpc_urls: list[str] = Field(default_factory=lambda: ["https://rpc.mantle.xyz"])
    morph_rpc_urls: list[str] = Field(default_factory=lambda: ["https://rpc.morphl2.io"])

    @field_validator("*", mode="after")
    @classmethod
    def validate_urls(cls, values: list[str]) -> list[str]:
        if not values:
            raise ValueError("multichain RPC URL lists must not be empty")
        if any(urlsplit(value).scheme != "https" or not urlsplit(value).netloc for value in values):
            raise ValueError("multichain RPC URLs must be valid HTTPS URLs")
        return values


class SupplyConfig(StrictModel):
    interval_seconds: int = Field(default=3600, gt=0)
    defillama_url: str = "https://stablecoins.llama.fi/stablecoins"
    expected_symbol: str = "USD1"
    expected_name: str = "World Liberty Financial USD"
    multichain: MultichainSupplyConfig = Field(default_factory=MultichainSupplyConfig)
```

Apply environment overrides by mapping field names to environment keys, rebuilding `MultichainSupplyConfig` through `model_validate`, and putting the validated value into `config.supply`:

```python
multichain_env = {
    "tron_rpc_urls": "TRON_RPC_URLS",
    "solana_rpc_urls": "SOLANA_RPC_URLS",
    "aptos_indexer_urls": "APTOS_INDEXER_URLS",
    "tempo_rpc_urls": "TEMPO_RPC_URLS",
    "plume_rpc_urls": "PLUME_RPC_URLS",
    "ab_rpc_urls": "AB_RPC_URLS",
    "monad_rpc_urls": "MONAD_RPC_URLS",
    "mantle_rpc_urls": "MANTLE_RPC_URLS",
    "morph_rpc_urls": "MORPH_RPC_URLS",
}
multichain_values = config.supply.multichain.model_dump()
for field_name, environment_name in multichain_env.items():
    urls = _rpc_urls_from_environment(values.get(environment_name))
    if urls:
        multichain_values[field_name] = urls
multichain = MultichainSupplyConfig.model_validate(multichain_values)
supply = config.supply.model_copy(update={"multichain": multichain})
```

Include `"supply": supply` in the final `AppConfig.model_copy(update=...)` call.

- [ ] **Step 5: 运行资产与配置测试**

Run: `python -m pytest tests/test_supply_assets.py tests/test_config.py -q`

Expected: PASS。

- [ ] **Step 6: 提交资产和配置基础**

```bash
git add usd1_monitor/supply_assets.py usd1_monitor/config.py tests/test_supply_assets.py tests/test_config.py
git commit -m "配置完整多链供应量数据源"
```

### Task 2: 实现 EVM 当前状态读取器

**Files:**
- Modify: `usd1_monitor/collectors/supply.py`
- Create: `usd1_monitor/collectors/multichain_evm.py`
- Create: `tests/test_multichain_evm.py`

- [ ] **Step 1: 写入固定精度、同链区块复用和部分失败测试**

```python
# tests/test_multichain_evm.py
def hex_word(value: int) -> str:
    return f"0x{value:064x}"


@pytest.mark.asyncio
async def test_evm_chain_reads_supply_and_pool_at_one_block() -> None:
    rpc = FakeRpc()
    rpc.result("eth_blockNumber", "0x64")
    rpc.result("eth_call", hex_word(5_000_000 * 10**18))
    rpc.result("eth_call", hex_word(1_250_000 * 10**18))
    collector = EvmChainSupplyCollector(
        "ethereum",
        rpc,
        specs=(NATIVE_ETHEREUM, LOCKED_ETHEREUM),
        confirmation_depth=3,
    )

    batch = await collector.collect(NOW)

    assert [item.supply for item in batch.snapshots] == [5_000_000, 1_250_000]
    assert all(call[1][-1] == "0x61" for call in rpc.calls if call[0] == "eth_call")
    assert len(rpc.calls_for("eth_blockNumber")) == 1
    assert not rpc.calls_for("eth_getLogs")
    assert not any("decimals" in str(params) for params in rpc.calls_for("eth_call"))


@pytest.mark.asyncio
async def test_evm_chain_keeps_successful_component_when_pool_call_fails() -> None:
    rpc = FakeRpc()
    rpc.result("eth_blockNumber", "0x64")
    rpc.result("eth_call", hex_word(5_000_000 * 10**18))
    rpc.result("eth_call", RpcError("pool unavailable"))
    collector = EvmChainSupplyCollector(
        "ethereum", rpc,
        specs=(NATIVE_ETHEREUM, LOCKED_ETHEREUM),
        confirmation_depth=3,
    )

    batch = await collector.collect(NOW)

    assert [item.scope for item in batch.snapshots] == ["ethereum"]
    assert batch.errors[0][0] == "locked_ethereum"
```

- [ ] **Step 2: 运行测试并确认读取器不存在**

Run: `python -m pytest tests/test_multichain_evm.py -q`

Expected: FAIL，无法导入 `EvmChainSupplyCollector`。

- [ ] **Step 3: 实现单链读取批次**

```python
# usd1_monitor/collectors/supply.py
@dataclass(frozen=True)
class ComponentBatch:
    snapshots: tuple[SupplySnapshot, ...]
    errors: tuple[tuple[str, Exception], ...] = ()


class ComponentSource(Protocol):
    component_ids: frozenset[str]

    async def collect(self, collected_at: datetime) -> ComponentBatch: ...
```

```python
# usd1_monitor/collectors/multichain_evm.py
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from eth_abi import encode

from usd1_monitor.collectors.reserves import selector
from usd1_monitor.collectors.supply import ComponentBatch, SupplyDataError, SupplySnapshot, _decode_uint
from usd1_monitor.models import Observation
from usd1_monitor.supply_assets import EvmSupplySpec


class RpcClient(Protocol):
    async def call(self, method: str, params: list) -> object: ...


class EvmChainSupplyCollector:
    def __init__(self, chain: str, rpc: RpcClient, *, specs: tuple[EvmSupplySpec, ...], confirmation_depth: int = 0) -> None:
        self.chain = chain
        self._rpc = rpc
        self._specs = specs
        self._confirmation_depth = confirmation_depth
        self.component_ids = frozenset(item.component_id for item in specs)

    async def collect(self, collected_at: datetime) -> ComponentBatch:
        latest_raw = await self._rpc.call("eth_blockNumber", [])
        try:
            block = int(str(latest_raw), 16) - self._confirmation_depth
        except (TypeError, ValueError) as exc:
            raise SupplyDataError(f"{self.chain} block number is malformed") from exc
        if block < 0:
            raise SupplyDataError(f"{self.chain} has no confirmed supply block")
        snapshots: list[SupplySnapshot] = []
        errors: list[tuple[str, Exception]] = []
        for spec in self._specs:
            try:
                snapshots.append(await self._read(spec, block, collected_at))
            except Exception as exc:
                errors.append((spec.component_id, exc))
        return ComponentBatch(tuple(snapshots), tuple(errors))

    async def _read(self, spec: EvmSupplySpec, block: int, collected_at: datetime) -> SupplySnapshot:
        if spec.holder_address is None:
            data = selector("totalSupply()")
        else:
            data = selector("balanceOf(address)") + encode(
                ["address"], [spec.holder_address]
            ).hex()
        raw = await self._rpc.call(
            "eth_call", [{"to": spec.token_address, "data": data}, hex(block)]
        )
        amount = _decode_uint(raw, "uint256", spec.component_id) / (10**spec.decimals)
        if amount < 0 or (spec.metric == "supply.native" and amount == 0):
            raise SupplyDataError(f"{spec.component_id} amount is invalid")
        observation = Observation(
            spec.metric, "evm_rpc", spec.scope, float(amount), "USD1",
            collected_at, collected_at, quality="FACT",
            metadata={
                "block": block,
                "decimals": spec.decimals,
                "component_id": spec.component_id,
                "source_url": spec.explorer_url,
            },
        )
        return SupplySnapshot(spec.scope, float(amount), collected_at, observation)
```

- [ ] **Step 4: 运行 EVM 读取器和原有供应量测试**

Run: `python -m pytest tests/test_multichain_evm.py tests/test_supply_collector.py -q`

Expected: PASS；原 `NativeSupplyCollector` 的兼容测试保持通过。

- [ ] **Step 5: 提交 EVM 多链读取器**

```bash
git add usd1_monitor/collectors/supply.py usd1_monitor/collectors/multichain_evm.py usd1_monitor/supply_assets.py tests/test_multichain_evm.py
git commit -m "读取EVM多链供应量与桥池余额"
```

### Task 3: 实现 Tron、Solana 和 Aptos 读取器

**Files:**
- Create: `usd1_monitor/collectors/non_evm_supply.py`
- Create: `tests/test_non_evm_supply.py`

- [ ] **Step 1: 写入三个协议的响应解析测试**

```python
# tests/test_non_evm_supply.py
def solana_mint_response(amount: str, decimals: int) -> dict:
    return {
        "value": {"data": {"parsed": {"info": {
            "supply": amount,
            "decimals": decimals,
        }}}}
    }


def solana_pool_response(amount: str, decimals: int) -> dict:
    return {
        "value": {"data": {"parsed": {"info": {
            "mint": SOLANA_MINT,
            "tokenAmount": {"amount": amount, "decimals": decimals},
        }}}}
    }


def aptos_response(*, supply: str, balance: str) -> dict:
    return {
        "data": {
            "fungible_asset_metadata": [{
                "asset_type": APTOS_METADATA,
                "decimals": 6,
                "supply_v2": supply,
            }],
            "current_fungible_asset_balances": [{
                "asset_type": APTOS_METADATA,
                "owner_address": APTOS_POOL,
                "amount": balance,
            }],
        }
    }


@pytest.mark.asyncio
async def test_tron_total_supply_uses_constant_contract() -> None:
    http = FakeHttp()
    url = "https://tron.example/wallet/triggerconstantcontract"
    http.queue_json(url, {
        "result": {"result": True},
        "constant_result": [f"{4_200_000 * 10**18:064x}"],
    }, method="POST")

    batch = await TronSupplyCollector(http, ["https://tron.example"]).collect(NOW)

    assert batch.snapshots[0].supply == 4_200_000
    payload = http.calls[0][2]
    assert payload["function_selector"] == "totalSupply()"
    assert payload["contract_address"] == TRON_TOKEN
    assert payload["visible"] is True


@pytest.mark.asyncio
async def test_solana_reads_mint_and_pool_with_get_account_info() -> None:
    rpc = FakeRpc()
    rpc.result("getAccountInfo", solana_mint_response("4200000000000", 6))
    rpc.result("getAccountInfo", solana_pool_response("1250000000000", 6))

    batch = await SolanaSupplyCollector(rpc).collect(NOW)

    assert [item.supply for item in batch.snapshots] == [4_200_000, 1_250_000]
    assert rpc.calls_for("getAccountInfo") == [
        [SOLANA_MINT, {"encoding": "jsonParsed", "commitment": "finalized"}],
        [SOLANA_POOL_TOKEN_ACCOUNT, {"encoding": "jsonParsed", "commitment": "finalized"}],
    ]


@pytest.mark.asyncio
async def test_aptos_reads_supply_and_pool_in_one_graphql_response() -> None:
    http = FakeHttp()
    url = "https://aptos.example/v1/graphql"
    http.queue_json(url, aptos_response(supply="4200000000000", balance="1250000000000"), method="POST")

    batch = await AptosSupplyCollector(http, [url]).collect(NOW)

    assert [item.supply for item in batch.snapshots] == [4_200_000, 1_250_000]
    assert len(http.calls) == 1
```

```python
@pytest.mark.asyncio
async def test_tron_missing_constant_result_marks_native_component_failed() -> None:
    http = FakeHttp()
    url = "https://tron.example/wallet/triggerconstantcontract"
    http.queue_json(url, {"result": {"result": True}}, method="POST")
    batch = await TronSupplyCollector(http, ["https://tron.example"]).collect(NOW)
    assert [item[0] for item in batch.errors] == ["native_tron"]


@pytest.mark.asyncio
async def test_solana_wrong_decimals_only_fails_affected_component() -> None:
    rpc = FakeRpc()
    rpc.result("getAccountInfo", solana_mint_response("4200000000000", 9))
    rpc.result("getAccountInfo", solana_pool_response("1250000000000", 6))
    batch = await SolanaSupplyCollector(rpc).collect(NOW)
    assert [item[0] for item in batch.errors] == ["native_solana"]
    assert [item.observation.metadata["component_id"] for item in batch.snapshots] == ["locked_solana"]


@pytest.mark.asyncio
async def test_aptos_duplicate_metadata_preserves_valid_pool_balance() -> None:
    http = FakeHttp()
    url = "https://aptos.example/v1/graphql"
    body = aptos_response(supply="4200000000000", balance="1250000000000")
    body["data"]["fungible_asset_metadata"] *= 2
    http.queue_json(url, body, method="POST")
    batch = await AptosSupplyCollector(http, [url]).collect(NOW)
    assert [item[0] for item in batch.errors] == ["native_aptos"]
    assert [item.observation.metadata["component_id"] for item in batch.snapshots] == ["locked_aptos"]


@pytest.mark.asyncio
async def test_tron_uses_second_endpoint_after_network_error() -> None:
    first = "https://tron-one.example/wallet/triggerconstantcontract"
    second = "https://tron-two.example/wallet/triggerconstantcontract"
    http = FakeHttp()
    http.queue_error(first, TimeoutError("timeout"), method="POST")
    http.queue_json(second, {
        "result": {"result": True},
        "constant_result": [f"{4_200_000 * 10**18:064x}"],
    }, method="POST")
    batch = await TronSupplyCollector(
        http, ["https://tron-one.example", "https://tron-two.example"]
    ).collect(NOW)
    assert not batch.errors
```

```python
@pytest.mark.asyncio
async def test_solana_wrong_pool_mint_fails_locked_component() -> None:
    rpc = FakeRpc()
    rpc.result("getAccountInfo", solana_mint_response("4200000000000", 6))
    pool = solana_pool_response("1250000000000", 6)
    pool["value"]["data"]["parsed"]["info"]["mint"] = "wrong"
    rpc.result("getAccountInfo", pool)
    batch = await SolanaSupplyCollector(rpc).collect(NOW)
    assert [item[0] for item in batch.errors] == ["locked_solana"]


@pytest.mark.asyncio
async def test_aptos_null_supply_preserves_valid_pool_balance() -> None:
    http = FakeHttp()
    url = "https://aptos.example/v1/graphql"
    body = aptos_response(supply="4200000000000", balance="1250000000000")
    body["data"]["fungible_asset_metadata"][0]["supply_v2"] = None
    http.queue_json(url, body, method="POST")
    batch = await AptosSupplyCollector(http, [url]).collect(NOW)
    assert [item[0] for item in batch.errors] == ["native_aptos"]
    assert len(batch.snapshots) == 1


@pytest.mark.asyncio
async def test_aptos_negative_pool_amount_fails_locked_component() -> None:
    http = FakeHttp()
    url = "https://aptos.example/v1/graphql"
    body = aptos_response(supply="4200000000000", balance="1250000000000")
    body["data"]["current_fungible_asset_balances"][0]["amount"] = "-1"
    http.queue_json(url, body, method="POST")
    batch = await AptosSupplyCollector(http, [url]).collect(NOW)
    assert [item[0] for item in batch.errors] == ["locked_aptos"]
    assert len(batch.snapshots) == 1
```

- [ ] **Step 2: 运行测试并确认非 EVM 读取器不存在**

Run: `python -m pytest tests/test_non_evm_supply.py -q`

Expected: FAIL，无法导入三个 collector。

- [ ] **Step 3: 实现公共构造和 HTTP 端点回退**

```python
# usd1_monitor/collectors/non_evm_supply.py
from typing import Protocol


class JsonPoster(Protocol):
    async def post_json(self, url: str, payload: dict) -> object: ...


class RpcClient(Protocol):
    async def call(self, method: str, params: list) -> object: ...


async def _post_with_fallback(
    http: JsonPoster,
    urls: tuple[str, ...],
    path: str,
    payload: dict,
) -> object:
    failures: list[str] = []
    for base_url in urls:
        url = f"{base_url.rstrip('/')}/{path.lstrip('/')}" if path else base_url
        try:
            return await http.post_json(url, payload)
        except Exception as exc:
            failures.append(f"{sanitize_url(url)}: {type(exc).__name__}")
    raise SupplyDataError("all non-EVM endpoints failed: " + " | ".join(failures))


def _snapshot(
    component_id: str,
    metric: str,
    source: str,
    scope: str,
    raw_amount: int,
    decimals: int,
    collected_at: datetime,
    source_url: str,
) -> SupplySnapshot:
    if raw_amount < 0 or (metric == "supply.native" and raw_amount == 0):
        raise SupplyDataError(f"{component_id} amount is invalid")
    value = raw_amount / (10**decimals)
    observation = Observation(
        metric, source, scope, float(value), "USD1", collected_at, collected_at,
        quality="FACT",
        metadata={
            "component_id": component_id,
            "decimals": decimals,
            "source_url": source_url,
        },
    )
    return SupplySnapshot(scope, float(value), collected_at, observation)
```

- [ ] **Step 4: 实现 Tron 常量合约读取**

```python
class TronSupplyCollector:
    component_ids = frozenset({"native_tron"})

    def __init__(self, http: JsonPoster, urls: list[str]) -> None:
        self._http = http
        self._urls = tuple(urls)

    async def collect(self, collected_at: datetime) -> ComponentBatch:
        try:
            body = await _post_with_fallback(
                self._http,
                self._urls,
                "wallet/triggerconstantcontract",
                {
                    "owner_address": TRON_TOKEN,
                    "contract_address": TRON_TOKEN,
                    "function_selector": "totalSupply()",
                    "parameter": "",
                    "visible": True,
                },
            )
            if not isinstance(body, dict) or body.get("result", {}).get("result") is not True:
                raise SupplyDataError("native_tron call was rejected")
            results = body.get("constant_result")
            if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], str):
                raise SupplyDataError("native_tron constant_result is malformed")
            raw = int(results[0], 16)
            snapshot = _snapshot(
                "native_tron", "supply.native", "tron", "tron", raw, 18,
                collected_at, f"https://tronscan.org/#/token20/{TRON_TOKEN}",
            )
            return ComponentBatch((snapshot,))
        except Exception as exc:
            return ComponentBatch((), (("native_tron", exc),))
```

- [ ] **Step 5: 实现 Solana `getAccountInfo` 解析**

```python
class SolanaSupplyCollector:
    component_ids = frozenset({"native_solana", "locked_solana"})

    def __init__(self, rpc: RpcClient) -> None:
        self._rpc = rpc

    async def collect(self, collected_at: datetime) -> ComponentBatch:
        specs = (
            ("native_solana", "supply.native", SOLANA_MINT, "supply"),
            ("locked_solana", "bridge.locked", SOLANA_POOL_TOKEN_ACCOUNT, "tokenAmount"),
        )
        snapshots: list[SupplySnapshot] = []
        errors: list[tuple[str, Exception]] = []
        for component_id, metric, address, field in specs:
            try:
                body = await self._rpc.call(
                    "getAccountInfo",
                    [address, {"encoding": "jsonParsed", "commitment": "finalized"}],
                )
                info = body["value"]["data"]["parsed"]["info"]
                amount = info[field] if field == "supply" else info[field]["amount"]
                decimals = info["decimals"] if field == "supply" else info[field]["decimals"]
                if decimals != 6 or not isinstance(amount, str) or not amount.isdigit():
                    raise SupplyDataError(f"{component_id} parsed amount is malformed")
                if field == "tokenAmount" and info.get("mint") != SOLANA_MINT:
                    raise SupplyDataError("locked_solana mint identity mismatch")
                snapshots.append(_snapshot(
                    component_id, metric, "solana_rpc", "solana", int(amount), 6,
                    collected_at, f"https://solscan.io/account/{address}",
                ))
            except Exception as exc:
                errors.append((component_id, exc))
        return ComponentBatch(tuple(snapshots), tuple(errors))
```

- [ ] **Step 6: 实现 Aptos 单次 GraphQL 查询**

```python
APTOS_QUERY = """
query Usd1Supply($asset: String!, $owner: String!) {
  fungible_asset_metadata(where: {asset_type: {_eq: $asset}}, limit: 2) {
    asset_type
    decimals
    supply_v2
  }
  current_fungible_asset_balances(
    where: {asset_type: {_eq: $asset}, owner_address: {_eq: $owner}},
    limit: 2
  ) {
    asset_type
    owner_address
    amount
  }
}
"""


class AptosSupplyCollector:
    component_ids = frozenset({"native_aptos", "locked_aptos"})

    def __init__(self, http: JsonPoster, urls: list[str]) -> None:
        self._http = http
        self._urls = tuple(urls)

    async def collect(self, collected_at: datetime) -> ComponentBatch:
        try:
            body = await _post_with_fallback(
                self._http, self._urls, "",
                {"query": APTOS_QUERY, "variables": {
                    "asset": APTOS_METADATA, "owner": APTOS_POOL,
                }},
            )
        except Exception as exc:
            return ComponentBatch((), (("native_aptos", exc), ("locked_aptos", exc)))

        if not isinstance(body, dict) or body.get("errors"):
            exc = SupplyDataError("Aptos indexer response contains errors")
            return ComponentBatch((), (("native_aptos", exc), ("locked_aptos", exc)))
        data = body.get("data")
        if not isinstance(data, dict):
            exc = SupplyDataError("Aptos indexer data is malformed")
            return ComponentBatch((), (("native_aptos", exc), ("locked_aptos", exc)))

        snapshots: list[SupplySnapshot] = []
        errors: list[tuple[str, Exception]] = []
        metadata = data.get("fungible_asset_metadata")
        try:
            if not isinstance(metadata, list) or len(metadata) != 1:
                raise SupplyDataError("Aptos USD1 metadata row must be unique")
            row = metadata[0]
            if not isinstance(row, dict) or row.get("asset_type") != APTOS_METADATA or row.get("decimals") != 6:
                raise SupplyDataError("Aptos USD1 metadata identity mismatch")
            raw_supply = row.get("supply_v2")
            if not isinstance(raw_supply, str) or not raw_supply.isdigit():
                raise SupplyDataError("Aptos USD1 supply_v2 is malformed")
            snapshots.append(_snapshot(
                "native_aptos", "supply.native", "aptos_indexer", "aptos",
                int(raw_supply), 6, collected_at, APTOS_EXPLORER,
            ))
        except Exception as exc:
            errors.append(("native_aptos", exc))

        balances = data.get("current_fungible_asset_balances")
        try:
            if not isinstance(balances, list) or len(balances) != 1:
                raise SupplyDataError("Aptos USD1 pool row must be unique")
            row = balances[0]
            if not isinstance(row, dict) or row.get("asset_type") != APTOS_METADATA or row.get("owner_address") != APTOS_POOL:
                raise SupplyDataError("Aptos USD1 pool identity mismatch")
            raw_balance = row.get("amount")
            if not isinstance(raw_balance, str) or not raw_balance.isdigit():
                raise SupplyDataError("Aptos USD1 pool amount is malformed")
            snapshots.append(_snapshot(
                "locked_aptos", "bridge.locked", "aptos_indexer", "aptos",
                int(raw_balance), 6, collected_at, APTOS_EXPLORER,
            ))
        except Exception as exc:
            errors.append(("locked_aptos", exc))
        return ComponentBatch(tuple(snapshots), tuple(errors))
```

- [ ] **Step 7: 运行非 EVM 和 HTTP/RPC 回归测试**

Run: `python -m pytest tests/test_non_evm_supply.py tests/test_http.py tests/test_rpc.py -q`

Expected: PASS。

- [ ] **Step 8: 提交非 EVM 读取器**

```bash
git add usd1_monitor/collectors/non_evm_supply.py tests/test_non_evm_supply.py
git commit -m "读取非EVM供应量与桥池余额"
```

### Task 4: 编排 16 个必需组件并生成完整快照

**Files:**
- Create: `usd1_monitor/collectors/multichain_supply.py`
- Create: `tests/test_multichain_supply.py`

- [ ] **Step 1: 写入完整聚合、缺项拒绝和并发上限测试**

```python
# tests/test_multichain_supply.py
def component_snapshot(
    component_id: str, metric: str, scope: str, value: float
) -> SupplySnapshot:
    observation = Observation(
        metric, "test", scope, value, "USD1", NOW, NOW,
        metadata={"component_id": component_id},
    )
    return SupplySnapshot(scope, value, NOW, observation)


class StaticSource:
    def __init__(self, snapshot: SupplySnapshot) -> None:
        self.snapshot = snapshot
        self.component_ids = frozenset({snapshot.observation.metadata["component_id"]})

    async def collect(self, collected_at: datetime) -> ComponentBatch:
        return ComponentBatch((self.snapshot,))


class FailingSource:
    def __init__(self, component_id: str) -> None:
        self.component_ids = frozenset({component_id})

    async def collect(self, collected_at: datetime) -> ComponentBatch:
        return ComponentBatch((), ((next(iter(self.component_ids)), RuntimeError("failed")),))


def component_sources_for(
    *, native: list[float], bridged: list[float], locked: list[float]
) -> list[StaticSource]:
    native_scopes = ("ethereum", "bsc", "tron", "solana", "aptos", "tempo")
    bridged_scopes = ("plume", "ab", "monad", "mantle", "morph")
    locked_scopes = ("ethereum", "bsc", "solana", "aptos", "tempo")
    snapshots = [
        *(component_snapshot(f"native_{scope}", "supply.native", scope, value)
          for scope, value in zip(native_scopes, native, strict=True)),
        *(component_snapshot(f"bridged_{scope}", "supply.bridged", scope, value)
          for scope, value in zip(bridged_scopes, bridged, strict=True)),
        *(component_snapshot(f"locked_{scope}", "bridge.locked", scope, value)
          for scope, value in zip(locked_scopes, locked, strict=True)),
    ]
    return [StaticSource(snapshot) for snapshot in snapshots]


def component_sources_for_complete_batch() -> list[StaticSource]:
    return component_sources_for(
        native=[100, 200, 300, 400, 500, 600],
        bridged=[10, 20, 30, 40, 50],
        locked=[11, 21, 31, 41, 51],
    )


class ConcurrencyGauge:
    def __init__(self) -> None:
        self.current = 0
        self.maximum = 0
        self.release = asyncio.Event()


class WaitingSource:
    def __init__(self, gauge: ConcurrencyGauge, component_id: str) -> None:
        self.gauge = gauge
        self.component_ids = frozenset({component_id})

    async def collect(self, collected_at: datetime) -> ComponentBatch:
        self.gauge.current += 1
        self.gauge.maximum = max(self.gauge.maximum, self.gauge.current)
        if self.gauge.maximum == 4:
            self.gauge.release.set()
        await self.gauge.release.wait()
        await asyncio.sleep(0)
        self.gauge.current -= 1
        return ComponentBatch(())


@pytest.mark.asyncio
async def test_complete_batch_aggregates_without_double_counting_bridged() -> None:
    source = MultichainSupplySource(component_sources_for(
        native=[100, 200, 300, 400, 500, 600],
        bridged=[10, 20, 30, 40, 50],
        locked=[11, 21, 31, 41, 51],
    ))

    batch = await source.collect(NOW)

    assert batch.complete is True
    totals = {item.observation.metric: item.supply for item in batch.totals}
    assert totals == {
        "supply.multichain_total": 2_100,
        "supply.bridged_total": 150,
        "bridge.locked_total": 155,
        "bridge.issuance_delta": -5,
    }


@pytest.mark.asyncio
async def test_one_missing_component_prevents_all_aggregate_values() -> None:
    sources = component_sources_for_complete_batch()
    sources[-1] = FailingSource("locked_aptos")

    batch = await MultichainSupplySource(sources).collect(NOW)

    assert batch.complete is False
    assert batch.totals == ()
    assert batch.errors[0][0] == "locked_aptos"


@pytest.mark.asyncio
async def test_source_concurrency_never_exceeds_four() -> None:
    gauge = ConcurrencyGauge()
    sources = [WaitingSource(gauge, f"source_{index}") for index in range(11)]

    await MultichainSupplySource(
        sources, required_component_ids=frozenset()
    ).collect(NOW)

    assert gauge.maximum == 4
```

- [ ] **Step 2: 运行测试并确认编排器不存在**

Run: `python -m pytest tests/test_multichain_supply.py -q`

Expected: FAIL，无法导入 `MultichainSupplySource`。

- [ ] **Step 3: 实现严格组件集合和聚合观察**

```python
# usd1_monitor/collectors/multichain_supply.py
REQUIRED_COMPONENT_IDS = frozenset({
    "native_ethereum", "native_bsc", "native_tron", "native_solana",
    "native_aptos", "native_tempo",
    "bridged_plume", "bridged_ab", "bridged_monad", "bridged_mantle",
    "bridged_morph",
    "locked_ethereum", "locked_bsc", "locked_solana", "locked_aptos",
    "locked_tempo",
})


@dataclass(frozen=True)
class MultichainSupplyBatch:
    components: tuple[SupplySnapshot, ...]
    totals: tuple[SupplySnapshot, ...]
    errors: tuple[tuple[str, Exception], ...]
    complete: bool


class MultichainSupplySource:
    def __init__(
        self,
        sources: list[ComponentSource],
        *,
        required_component_ids: frozenset[str] = REQUIRED_COMPONENT_IDS,
    ) -> None:
        self._sources = tuple(sources)
        self._required = required_component_ids
        self._semaphore = asyncio.Semaphore(4)

    @property
    def required_component_ids(self) -> frozenset[str]:
        return self._required

    @property
    def max_concurrency(self) -> int:
        return 4

    async def _collect_one(self, source: ComponentSource, now: datetime) -> ComponentBatch:
        async with self._semaphore:
            return await source.collect(now)

    async def collect(self, collected_at: datetime) -> MultichainSupplyBatch:
        results = await asyncio.gather(
            *(self._collect_one(source, collected_at) for source in self._sources),
            return_exceptions=True,
        )
        components: list[SupplySnapshot] = []
        errors: list[tuple[str, Exception]] = []
        for source, result in zip(self._sources, results, strict=True):
            if isinstance(result, Exception):
                errors.extend((item, result) for item in source.component_ids)
            elif isinstance(result, BaseException):
                raise result
            else:
                components.extend(result.snapshots)
                errors.extend(result.errors)
        indexed = {
            str(item.observation.metadata["component_id"]): item
            for item in components
        }
        complete = (
            not errors
            and len(components) == len(self._required)
            and set(indexed) == set(self._required)
        )
        totals = self._totals(indexed, collected_at) if complete else ()
        return MultichainSupplyBatch(
            tuple(components), tuple(totals), tuple(errors), complete
        )

    @staticmethod
    def _totals(
        indexed: dict[str, SupplySnapshot], collected_at: datetime
    ) -> tuple[SupplySnapshot, ...]:
        native = math.fsum(
            item.supply for component_id, item in indexed.items()
            if component_id.startswith("native_")
        )
        bridged = math.fsum(
            item.supply for component_id, item in indexed.items()
            if component_id.startswith("bridged_")
        )
        locked = math.fsum(
            item.supply for component_id, item in indexed.items()
            if component_id.startswith("locked_")
        )
        if native <= 0 or bridged < 0 or locked < 0:
            raise SupplyDataError("multichain aggregate values are invalid")

        def aggregate(metric: str, value: float) -> SupplySnapshot:
            observation = Observation(
                metric, "onchain_multichain", "global", value, "USD1",
                collected_at, collected_at, quality="FACT",
                metadata={"components": sorted(indexed)},
            )
            return SupplySnapshot("global", value, collected_at, observation)

        return (
            aggregate("supply.multichain_total", native),
            aggregate("supply.bridged_total", bridged),
            aggregate("bridge.locked_total", locked),
            aggregate("bridge.issuance_delta", bridged - locked),
        )
```

- [ ] **Step 4: 运行编排器与所有采集器测试**

Run: `python -m pytest tests/test_multichain_supply.py tests/test_multichain_evm.py tests/test_non_evm_supply.py -q`

Expected: PASS。

- [ ] **Step 5: 提交多链编排器**

```bash
git add usd1_monitor/collectors/multichain_supply.py tests/test_multichain_supply.py
git commit -m "聚合完整多链供应量快照"
```

### Task 5: 实现桥接差额确认和恢复规则

**Files:**
- Modify: `usd1_monitor/engine/supply_rules.py`
- Modify: `tests/test_supply_rules.py`

- [ ] **Step 1: 写入正负差额、边界、升级和恢复测试**

```python
# tests/test_supply_rules.py
def bridge(issued: float, locked: float) -> BridgeReading:
    return BridgeReading(issued=issued, locked=locked)


def test_overissued_requires_two_points_above_both_yellow_thresholds() -> None:
    one = evaluate_bridge_reconciliation(
        [bridge(100_200_000, 100_000_000)], RiskLevel.GREEN
    )
    two = evaluate_bridge_reconciliation(
        [bridge(100_200_000, 100_000_000)] * 2, RiskLevel.GREEN
    )
    assert one.level is RiskLevel.GREEN
    assert two.level is RiskLevel.YELLOW
    assert two.direction is BridgeDirection.OVERISSUED


def test_red_requires_two_red_points_not_one_yellow_then_one_red() -> None:
    result = evaluate_bridge_reconciliation(
        [bridge(100_200_000, 100_000_000), bridge(102_000_000, 100_000_000)],
        RiskLevel.YELLOW,
    )
    assert result.level is RiskLevel.YELLOW


def test_locked_excess_never_becomes_red() -> None:
    result = evaluate_bridge_reconciliation(
        [bridge(100_000_000, 102_000_000)] * 2, RiskLevel.GREEN
    )
    assert result.level is RiskLevel.YELLOW
    assert result.direction is BridgeDirection.LOCKED_EXCESS


def test_abnormal_state_needs_two_normal_points_to_recover() -> None:
    first = evaluate_bridge_reconciliation(
        [bridge(102_000_000, 100_000_000), bridge(100_050_000, 100_000_000)],
        RiskLevel.YELLOW,
    )
    second = evaluate_bridge_reconciliation(
        [bridge(100_050_000, 100_000_000)] * 2,
        RiskLevel.YELLOW,
    )
    assert first.level is RiskLevel.YELLOW
    assert second.level is RiskLevel.GREEN
```

```python
@pytest.mark.parametrize(
    ("reading", "expected"),
    [
        (bridge(100_100_000, 100_000_000), RiskLevel.GREEN),
        (bridge(101_000_000, 100_000_000), RiskLevel.YELLOW),
        (bridge(100_000_000, 101_000_000), RiskLevel.GREEN),
    ],
)
def test_bridge_exact_boundaries_follow_strict_severity_thresholds(
    reading: BridgeReading, expected: RiskLevel,
) -> None:
    result = evaluate_bridge_reconciliation(
        [reading, reading], RiskLevel.GREEN
    )
    assert result.level is expected


def test_zero_denominators_follow_approved_asymmetric_rules() -> None:
    overissued = evaluate_bridge_reconciliation(
        [bridge(1, 0), bridge(1, 0)], RiskLevel.GREEN
    )
    locked_excess = evaluate_bridge_reconciliation(
        [bridge(0, 1_000_001), bridge(0, 1_000_001)], RiskLevel.GREEN
    )
    assert overissued.level is RiskLevel.RED
    assert locked_excess.level is RiskLevel.YELLOW


def test_direction_change_restarts_confirmation() -> None:
    result = evaluate_bridge_reconciliation(
        [
            bridge(102_000_000, 100_000_000),
            bridge(100_000_000, 102_000_000),
        ],
        RiskLevel.GREEN,
    )
    assert result.level is RiskLevel.GREEN
```

- [ ] **Step 2: 运行供应量规则测试并确认新类型不存在**

Run: `python -m pytest tests/test_supply_rules.py -q`

Expected: FAIL，无法导入桥接规则类型。

- [ ] **Step 3: 实现纯函数状态机**

```python
# usd1_monitor/engine/supply_rules.py
class BridgeDirection(StrEnum):
    NORMAL = "normal"
    OVERISSUED = "overissued"
    LOCKED_EXCESS = "locked_excess"


@dataclass(frozen=True)
class BridgeReading:
    issued: float
    locked: float


@dataclass(frozen=True)
class BridgeEvaluation:
    level: RiskLevel
    direction: BridgeDirection
    delta: float
    ratio_percent: float | None


def _classify_bridge(reading: BridgeReading) -> BridgeEvaluation:
    delta = reading.issued - reading.locked
    if delta > 0:
        ratio = None if reading.locked == 0 else delta / reading.locked * 100
        if reading.locked == 0:
            level = RiskLevel.RED
        elif delta > 1_000_000 and ratio > 1:
            level = RiskLevel.RED
        elif delta > 100_000 and ratio > 0.1:
            level = RiskLevel.YELLOW
        else:
            level = RiskLevel.GREEN
        direction = BridgeDirection.OVERISSUED
    elif delta < 0:
        magnitude = -delta
        ratio = None if reading.issued == 0 else magnitude / reading.issued * 100
        level = (
            RiskLevel.YELLOW
            if magnitude > 1_000_000 and (ratio is None or ratio > 1)
            else RiskLevel.GREEN
        )
        direction = BridgeDirection.LOCKED_EXCESS
    else:
        level = RiskLevel.GREEN
        direction = BridgeDirection.NORMAL
        ratio = 0.0
    return BridgeEvaluation(level, direction, delta, ratio)


def evaluate_bridge_reconciliation(
    readings: list[BridgeReading], previous_level: RiskLevel
) -> BridgeEvaluation:
    if not readings:
        return BridgeEvaluation(
            previous_level, BridgeDirection.NORMAL, 0.0, None
        )
    current = _classify_bridge(readings[-1])
    if len(readings) < 2:
        return BridgeEvaluation(
            previous_level, current.direction, current.delta,
            current.ratio_percent,
        )
    prior = _classify_bridge(readings[-2])
    same_direction = prior.direction is current.direction
    two_green = prior.level is RiskLevel.GREEN and current.level is RiskLevel.GREEN
    two_red = (
        same_direction
        and prior.level is RiskLevel.RED
        and current.level is RiskLevel.RED
    )
    two_warning = (
        same_direction
        and prior.level is not RiskLevel.GREEN
        and current.level is not RiskLevel.GREEN
    )
    if previous_level is RiskLevel.RED:
        level = RiskLevel.GREEN if two_green else RiskLevel.RED
    elif previous_level is RiskLevel.YELLOW:
        if two_red:
            level = RiskLevel.RED
        elif two_green:
            level = RiskLevel.GREEN
        else:
            level = RiskLevel.YELLOW
    elif two_red:
        level = RiskLevel.RED
    elif two_warning:
        level = RiskLevel.YELLOW
    else:
        level = RiskLevel.GREEN
    return BridgeEvaluation(
        level, current.direction, current.delta, current.ratio_percent
    )
```

- [ ] **Step 4: 运行全部供应量规则测试**

Run: `python -m pytest tests/test_supply_rules.py -q`

Expected: PASS，现有覆盖率和 24 小时下降规则不回归。

- [ ] **Step 5: 提交桥接风险规则**

```bash
git add usd1_monitor/engine/supply_rules.py tests/test_supply_rules.py
git commit -m "评估跨链发行与锁仓差额"
```

### Task 6: 将完整快照接入储备覆盖率和原子持久化

**Files:**
- Modify: `usd1_monitor/scheduler.py`
- Modify: `tests/fakes.py`
- Modify: `tests/test_reserve_supply_integration.py`

- [ ] **Step 1: 写入完整批次、残缺批次和覆盖率分母测试**

```python
# tests/test_reserve_supply_integration.py
from usd1_monitor.collectors.supply import SupplySnapshot


def por_snapshot(reserves: float, observed_at: datetime) -> PorSnapshot:
    observation = Observation(
        "por.reserves", "chainlink", "ethereum", reserves, "USD1",
        observed_at, observed_at,
    )
    return PorSnapshot(
        reserves, int(observed_at.timestamp()), observed_at, observed_at,
        (observation,),
    )


def aggregate_snapshot(metric: str, value: float) -> SupplySnapshot:
    observation = Observation(
        metric, "onchain_multichain", "global", value, "USD1", NOW, NOW,
    )
    return SupplySnapshot("global", value, NOW, observation)


def multichain_batch(
    *, native_total: float, bridged_total: float,
    locked_total: float, complete: bool,
) -> SupplyBatch:
    totals = (
        aggregate_snapshot("supply.multichain_total", native_total),
        aggregate_snapshot("supply.bridged_total", bridged_total),
        aggregate_snapshot("bridge.locked_total", locked_total),
        aggregate_snapshot(
            "bridge.issuance_delta", bridged_total - locked_total
        ),
    )
    return SupplyBatch(totals if complete else (), (), complete)


def partial_multichain_batch(*failed_ids: str) -> SupplyBatch:
    component = supply_snapshot("ethereum", 100, NOW)
    errors = tuple(
        (component_id, RuntimeError(f"{component_id} unavailable"))
        for component_id in failed_ids
    )
    return SupplyBatch((component,), errors, False)


@pytest.mark.asyncio
async def test_complete_multichain_batch_drives_coverage(storage) -> None:
    por = FakePorCollector()
    por.queue_snapshot(por_snapshot(4_200, NOW))
    supply = FakeSupplyCollector()
    supply.queue_batch(multichain_batch(
        native_total=4_000,
        bridged_total=1_000,
        locked_total=1_000,
        complete=True,
    ))
    monitor = ReserveSupplyMonitor(por, supply, storage, None)

    await monitor.check_once(deliver=False, now=NOW)

    ratio = await storage.latest_observation(
        "supply.estimated_collateralization", "global"
    )
    assert ratio is not None and ratio.value == 105.0
    assert ratio.source == "por+onchain_multichain"


@pytest.mark.asyncio
async def test_partial_batch_persists_components_without_aggregates_or_risk(storage) -> None:
    supply = FakeSupplyCollector()
    supply.queue_batch(partial_multichain_batch("locked_aptos"))
    monitor = ReserveSupplyMonitor(FakePorCollector(), supply, storage, None)

    await monitor.check_once(deliver=False, now=NOW)

    assert await storage.latest_observation("supply.native", "ethereum") is not None
    assert await storage.latest_observation("supply.multichain_total", "global") is None
    assert await storage.get_risk_state("supply.bridge_reconciliation") is None
```

```python
@pytest.mark.asyncio
async def test_por_only_cycle_does_not_duplicate_coverage_point(storage) -> None:
    por = FakePorCollector()
    por.queue_snapshot(por_snapshot(4_200, NOW))
    por.queue_snapshot(por_snapshot(4_200, NOW + timedelta(minutes=5)))
    supply = FakeSupplyCollector()
    supply.queue_batch(multichain_batch(
        native_total=4_000, bridged_total=1_000,
        locked_total=1_000, complete=True,
    ))
    monitor = ReserveSupplyMonitor(por, supply, storage, None)

    await monitor.check_once(deliver=False, now=NOW)
    await monitor.check_once(deliver=False, now=NOW + timedelta(minutes=5))

    rows = await storage.latest_observations(
        "supply.estimated_collateralization", limit=10
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_restart_due_time_ignores_recent_defillama_only_reading(storage) -> None:
    await storage.insert_observation(supply_snapshot("global", 4_000, NOW))
    supply = FakeSupplyCollector()
    supply.queue_batch(partial_multichain_batch("locked_aptos"))
    monitor = ReserveSupplyMonitor(FakePorCollector(), supply, storage, None)

    result = await monitor.check_once(
        deliver=False, now=NOW + timedelta(minutes=1)
    )

    assert any("locked_aptos" in error for error in result.errors)


@pytest.mark.asyncio
async def test_bridge_evaluation_failure_rolls_back_complete_totals(
    storage, monkeypatch
) -> None:
    supply = FakeSupplyCollector()
    supply.queue_batch(multichain_batch(
        native_total=4_000, bridged_total=1_100,
        locked_total=1_000, complete=True,
    ))
    monitor = ReserveSupplyMonitor(FakePorCollector(), supply, storage, None)

    async def fail_bridge(now: datetime) -> list[RuleEvaluation]:
        raise RuntimeError("bridge evaluation failed")

    monkeypatch.setattr(monitor, "_bridge_supply_evaluations", fail_bridge)
    await monitor.check_once(deliver=False, now=NOW)

    assert await storage.latest_observation(
        "supply.multichain_total", "global"
    ) is None
    assert await storage.latest_observation(
        "bridge.issuance_delta", "global"
    ) is None
```

- [ ] **Step 2: 运行集成测试并确认现有 monitor 仍依赖 DefiLlama**

Run: `python -m pytest tests/test_reserve_supply_integration.py -q`

Expected: FAIL，覆盖率仍读取 `supply.global` 或 `SupplyBatch` 不表达完整性。

- [ ] **Step 3: 扩展供应量批次并合并 DefiLlama 辅助结果**

```python
# usd1_monitor/scheduler.py
@dataclass(frozen=True)
class SupplyBatch:
    snapshots: tuple[SupplySnapshot, ...]
    errors: tuple[tuple[str, Exception], ...] = ()
    multichain_complete: bool = False


class CombinedSupplySource:
    def __init__(
        self,
        multichain_source: MultichainSupplySource,
        global_source: DefiLlamaSupplyCollector,
    ) -> None:
        self._multichain = multichain_source
        self._global = global_source

    async def collect(self, collected_at: datetime) -> SupplyBatch:
        multichain_result, global_result = await asyncio.gather(
            self._multichain.collect(collected_at),
            self._global.collect(collected_at),
            return_exceptions=True,
        )
        snapshots: list[SupplySnapshot] = []
        errors: list[tuple[str, Exception]] = []
        multichain_complete = False
        if isinstance(multichain_result, Exception):
            errors.append(("multichain", multichain_result))
        elif isinstance(multichain_result, BaseException):
            raise multichain_result
        else:
            snapshots.extend(multichain_result.components)
            snapshots.extend(multichain_result.totals)
            errors.extend(multichain_result.errors)
            multichain_complete = multichain_result.complete
        if isinstance(global_result, Exception):
            errors.append(("defillama", global_result))
        elif isinstance(global_result, BaseException):
            raise global_result
        else:
            snapshots.append(global_result)
        return SupplyBatch(
            tuple(snapshots), tuple(errors), multichain_complete
        )
```

Replace the old `CombinedSupplySource` constructor and update every in-repository caller and test in this task. Do not add a permanent compatibility branch for the old internal constructor.

- [ ] **Step 4: 让供应量持久化只在完整批次更新聚合风险**

Change `_persist_supplies` to receive the entire `SupplyBatch`. Insert all snapshots, then only when `batch.multichain_complete` is true:

```python
evaluations: list[RuleEvaluation] = []
if batch.multichain_complete:
    _, coverage_evaluations = await self._coverage_update(now)
    evaluations.extend(coverage_evaluations)
    evaluations.extend(await self._native_supply_evaluations(now))
    evaluations.extend(await self._bridge_supply_evaluations(now))
if evaluations:
    await StateEngine(self._storage).apply_uncommitted(evaluations, now)
```

Remove coverage updates from `_persist_por`; PoR persistence continues to evaluate only PoR age/change rules. In `_coverage_update`, replace `supply.global` with `supply.multichain_total` and set the observation source to `por+onchain_multichain`. Remove the `force`/replace path after updating its callers because one complete supply snapshot produces exactly one coverage point.

- [ ] **Step 5: 基于聚合值评估桥接与 24 小时下降**

`_bridge_supply_evaluations` reads the two latest `supply.bridged_total` and `bridge.locked_total` observations, pairs rows by identical `collected_at`, ignores gaps over 1.5 supply intervals, passes readings plus prior level to `evaluate_bridge_reconciliation`, and emits:

```python
RuleEvaluation(
    "supply.bridge_reconciliation",
    result.level,
    {
        "direction": result.direction.value,
        "issued": issued.value,
        "locked": locked.value,
        "difference": result.delta,
        "difference_percent": result.ratio_percent,
        "data_time": now.isoformat(),
    },
)
```

Replace `_native_drop_24h` row selection with `supply.multichain_total` current and at-or-before-24-hour observations. Preserve the existing freshness window and `max(0.0, ...)` behavior.

- [ ] **Step 6: 更新假采集器并运行储备供应量集成测试**

Add `FakeSupplyCollector.queue_batch(batch)` without removing `queue_global_supply`, so older tests remain readable while new tests supply explicit complete/partial batches.

Run: `python -m pytest tests/test_reserve_supply_integration.py tests/test_supply_rules.py -q`

Expected: PASS。

- [ ] **Step 7: 提交监控器集成**

```bash
git add usd1_monitor/scheduler.py tests/fakes.py tests/test_reserve_supply_integration.py
git commit -m "接入完整多链供应量风险核对"
```

### Task 7: 合并多链健康状态并提供简明微信消息

**Files:**
- Modify: `usd1_monitor/scheduler.py`
- Modify: `usd1_monitor/notifications/wechat.py`
- Modify: `tests/test_reserve_supply_integration.py`
- Modify: `tests/test_wechat.py`

- [ ] **Step 1: 写入单条健康通知和桥接文案测试**

```python
# tests/test_reserve_supply_integration.py
@pytest.mark.asyncio
async def test_three_partial_runs_enqueue_one_grouped_health_alert(storage) -> None:
    supply = FakeSupplyCollector()
    for _ in range(3):
        supply.queue_batch(partial_multichain_batch(
            "native_tron", "locked_aptos"
        ))
    monitor = ReserveSupplyMonitor(FakePorCollector(), supply, storage, None)

    for hour in range(3):
        await monitor.check_once(deliver=False, now=NOW + timedelta(hours=hour))

    pending = await storage.pending_alerts()
    assert len(pending) == 1
    assert "Tron" in pending[0].content
    assert "Aptos" in pending[0].content
    assert "供应量数据连续 3 次未能完整获取" in pending[0].content


# tests/test_wechat.py
def test_bridge_overissue_message_is_plain_chinese() -> None:
    message = format_transitions(
        [RiskTransition(
            rule_id="supply.bridge_reconciliation",
            previous=RiskLevel.GREEN,
            current=RiskLevel.YELLOW,
            changed_at=NOW,
            first_triggered_at=NOW,
            evidence={
                "direction": "overissued",
                "issued": 1_250_000_000,
                "locked": 1_249_650_000,
                "difference": 350_000,
                "difference_percent": 0.028,
                "data_time": NOW.isoformat(),
            },
        )]
    )

    assert "跨链发行量比桥池锁仓量多 35 万 USD1" in message
    assert "supply.bridge_reconciliation" not in message
    assert "threshold" not in message
```

```python
@pytest.mark.asyncio
async def test_grouped_supply_health_recovers_once_after_dwell(storage) -> None:
    supply = FakeSupplyCollector()
    for _ in range(3):
        supply.queue_batch(partial_multichain_batch("native_tron"))
    supply.queue_batch(multichain_batch(
        native_total=4_000, bridged_total=1_000,
        locked_total=1_000, complete=True,
    ))
    supply.queue_batch(multichain_batch(
        native_total=4_000, bridged_total=1_000,
        locked_total=1_000, complete=True,
    ))
    monitor = ReserveSupplyMonitor(
        FakePorCollector(), supply, storage, None,
        supply_config=SupplyConfig(interval_seconds=1),
    )

    for second in (0, 1, 2):
        await monitor.check_once(
            deliver=False, now=NOW + timedelta(seconds=second)
        )
    await monitor.check_once(
        deliver=False, now=NOW + timedelta(seconds=32)
    )
    assert len(await storage.pending_alerts()) == 1

    await monitor.check_once(
        deliver=False, now=NOW + timedelta(seconds=63)
    )
    pending = await storage.pending_alerts()
    assert len(pending) == 2
    assert "供应量数据获取已恢复" in pending[-1].content
```

- [ ] **Step 2: 运行通知测试并确认当前产生多个健康规则**

Run: `python -m pytest tests/test_reserve_supply_integration.py tests/test_wechat.py -q`

Expected: FAIL，健康状态逐来源排队或桥接规则落入通用文案。

- [ ] **Step 3: 支持静默维护单项健康并公开一个聚合规则**

Extend `_record_health` with `enqueue_alerts: bool = True` and `extra_evidence: dict[str, object] | None = None`; pass `enqueue_alerts` into `StateEngine.apply` and merge `extra_evidence` into its evidence dictionary.

During supply handling:

```python
for component_id in all_component_ids:
    await _record_health(
        self._storage,
        f"supply_{component_id}",
        checked_at,
        success=component_id not in failed_ids,
        error=errors_by_id.get(component_id),
        enqueue_alerts=False,
    )

await _record_health(
    self._storage,
    "supply_multichain",
    checked_at,
    success=not required_failed_ids,
    error="; ".join(errors_by_id[item] for item in sorted(required_failed_ids)) or None,
    extra_evidence={"failed_sources": sorted(required_failed_ids)},
)
```

Continue tracking DefiLlama under `supply_defillama`. Do not include a DefiLlama-only failure in `health.supply_multichain`, because it is auxiliary and does not invalidate the complete on-chain snapshot.

- [ ] **Step 4: 添加供应量来源名称和桥接风险文案**

```python
# usd1_monitor/notifications/wechat.py
SUPPLY_COMPONENT_LABELS = {
    "native_ethereum": "Ethereum 原生供应量",
    "native_bsc": "BNB Chain 原生供应量",
    "native_tron": "Tron 原生供应量",
    "native_solana": "Solana 原生供应量",
    "native_aptos": "Aptos 原生供应量",
    "native_tempo": "Tempo 原生供应量",
    "bridged_plume": "Plume 跨链发行量",
    "bridged_ab": "AB Core 跨链发行量",
    "bridged_monad": "Monad 跨链发行量",
    "bridged_mantle": "Mantle 跨链发行量",
    "bridged_morph": "Morph 跨链发行量",
    "locked_ethereum": "Ethereum 桥池余额",
    "locked_bsc": "BNB Chain 桥池余额",
    "locked_solana": "Solana 桥池余额",
    "locked_aptos": "Aptos 桥池余额",
    "locked_tempo": "Tempo 桥池余额",
}
```

Add special handling before the generic health branch for `health.supply_multichain`, listing the unique labels from `failed_sources`. Add special handling for `supply.bridge_reconciliation`: use `difference` absolute value, direction-specific summary, and issued/locked detail lines. Add `_format_usd1_amount` that formats exact multiples or rounded values in 亿/万 units without Markdown.

- [ ] **Step 5: 运行集成和微信测试**

Run: `python -m pytest tests/test_reserve_supply_integration.py tests/test_wechat.py tests/test_health.py -q`

Expected: PASS，每次状态变化最多一条多链供应量健康消息。

- [ ] **Step 6: 提交通知降噪**

```bash
git add usd1_monitor/scheduler.py usd1_monitor/notifications/wechat.py tests/test_reserve_supply_integration.py tests/test_wechat.py
git commit -m "合并多链供应量健康通知"
```

### Task 8: 接入生产构建、状态输出和部署配置

**Files:**
- Modify: `usd1_monitor/cli.py`
- Modify: `usd1_monitor/scheduler.py`
- Modify: `usd1_monitor/notifications/wechat.py`
- Modify: `config.example.yaml`
- Modify: `deploy/config.production.example.yaml`
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_examples.py`
- Modify: `tests/test_status_output.py`
- Modify: `tests/test_evm_integration.py`

- [ ] **Step 1: 写入生产 builder 和能力状态测试**

```python
# tests/test_cli.py
def test_builder_wires_all_multichain_supply_sources(config, storage) -> None:
    monitor, resource = build_market_monitor(config, storage)

    source = monitor._reserve_supply._supply._multichain  # noqa: SLF001
    assert source.required_component_ids == REQUIRED_COMPONENT_IDS
    assert source.max_concurrency == 4
    assert resource is not None


# tests/test_status_output.py
def test_full_multichain_reconciliation_is_no_longer_not_monitored() -> None:
    assert "full_multichain_supply_reconciliation" not in NOT_MONITORED
    assert "tron_solana_aptos_tempo_bridges" in NOT_MONITORED
```

```python
# tests/test_evm_integration.py, extend existing startup-message test
assert "完整多链供应量与桥接核对" in message
assert "暂未覆盖：\n完整多链供应量核对" not in message


# tests/test_status_output.py
@pytest.mark.asyncio
async def test_status_prints_multichain_aggregate_metrics(storage, capsys) -> None:
    now = datetime(2026, 9, 10, tzinfo=UTC)
    for metric, value in (
        ("supply.multichain_total", 4_200_000_000),
        ("supply.bridged_total", 1_250_000_000),
        ("bridge.locked_total", 1_249_650_000),
        ("bridge.issuance_delta", 350_000),
    ):
        await storage.insert_observation(Observation(
            metric, "onchain_multichain", "global", value, "USD1",
            now, now,
        ))

    await _print_status(storage)

    output = capsys.readouterr().out
    for metric in (
        "supply.multichain_total",
        "supply.bridged_total",
        "bridge.locked_total",
        "bridge.issuance_delta",
    ):
        assert f"metric {metric}: FACT" in output
```

- [ ] **Step 2: 运行 CLI、示例和状态测试并确认接线缺失**

Run: `python -m pytest tests/test_cli.py tests/test_examples.py tests/test_status_output.py tests/test_evm_integration.py -q`

Expected: FAIL，builder 仍只创建 Ethereum/BNB 原生供应量源，能力仍列为未覆盖。

- [ ] **Step 3: 在 builder 中构造所有客户端和采集器**

Build `JsonRpcClient` instances for Tempo、Plume、AB Core、Monad、Mantle、Morph with the chain IDs from `EVM_CHAIN_IDS`. Reuse existing Ethereum/BNB clients and their configured confirmation depths; use confirmation depth 0 for the six supply-only EVM networks.

Group EVM specs by chain and create eight `EvmChainSupplyCollector` instances. Then construct:

```python
multichain = MultichainSupplySource([
    *evm_supply_collectors,
    TronSupplyCollector(http, config.supply.multichain.tron_rpc_urls),
    SolanaSupplyCollector(JsonRpcClient(
        config.supply.multichain.solana_rpc_urls, http
    )),
    AptosSupplyCollector(
        http, config.supply.multichain.aptos_indexer_urls
    ),
])
reserve_supply = ReserveSupplyMonitor(
    confirmed_por,
    CombinedSupplySource(multichain, defillama),
    storage,
    None,
    por_config=config.por,
    supply_config=config.supply,
)
```

Do not add SDK dependencies; all new protocols use the existing HTTP and JSON-RPC clients.

- [ ] **Step 4: 更新 `check/status` 和启动能力名称**

Remove `full_multichain_supply_reconciliation` from `NOT_MONITORED` but leave `tron_solana_aptos_tempo_bridges`, because this feature reconciles balances and does not trace individual cross-chain transactions.

When reserve supply is configured, startup monitored items include both “储备与供应量” and “完整多链供应量与桥接核对”. `_print_status` adds the four global aggregate metrics and the 16 component metrics in stable native/bridged/locked order. `check` details use these concise forms:

```text
supply_native ethereum=...
supply_bridged plume=...
bridge_locked ethereum=...
supply_multichain total=...
bridge_reconciliation issued=... locked=... delta=...
```

- [ ] **Step 5: 更新示例配置和 README**

Add this exact mapping under `supply` in both YAML examples:

```yaml
  multichain:
    tron_rpc_urls:
      - https://api.trongrid.io
      - https://api.tronstack.io
    solana_rpc_urls:
      - https://solana-rpc.publicnode.com
      - https://api.mainnet-beta.solana.com
    aptos_indexer_urls:
      - https://api.mainnet.aptoslabs.com/v1/graphql
    tempo_rpc_urls: [https://rpc.presto.tempo.xyz]
    plume_rpc_urls: [https://rpc.plume.org]
    ab_rpc_urls: [https://rpc.core.ab.org]
    monad_rpc_urls: [https://rpc.monad.xyz]
    mantle_rpc_urls: [https://rpc.mantle.xyz]
    morph_rpc_urls: [https://rpc.morphl2.io]
```

Add these credential-free comments to `.env.example`:

```dotenv
# TRON_RPC_URLS=https://api.trongrid.io,https://api.tronstack.io
# SOLANA_RPC_URLS=https://solana-rpc.publicnode.com,https://api.mainnet-beta.solana.com
# APTOS_INDEXER_URLS=https://api.mainnet.aptoslabs.com/v1/graphql
# TEMPO_RPC_URLS=https://rpc.presto.tempo.xyz
# PLUME_RPC_URLS=https://rpc.plume.org
# AB_RPC_URLS=https://rpc.core.ab.org
# MONAD_RPC_URLS=https://rpc.monad.xyz
# MANTLE_RPC_URLS=https://rpc.mantle.xyz
# MORPH_RPC_URLS=https://rpc.morphl2.io
```

README changes:

- replace “PoR reserves / DefiLlama global supply” with the complete on-chain denominator;
- document the six native chains, five bridged chains and five pools;
- state the one-hour interval and less-than-30-response estimate;
- document comma-separated endpoint overrides;
- remove only `full_multichain_supply_reconciliation` from the NOT_MONITORED list;
- retain individual bridge transaction monitoring as not covered.

- [ ] **Step 6: 运行 CLI、配置示例、状态和启动测试**

Run: `python -m pytest tests/test_cli.py tests/test_examples.py tests/test_status_output.py tests/test_evm_integration.py -q`

Expected: PASS。

- [ ] **Step 7: 提交生产与部署接线**

```bash
git add usd1_monitor/cli.py usd1_monitor/scheduler.py usd1_monitor/notifications/wechat.py config.example.yaml deploy/config.production.example.yaml .env.example README.md tests/test_cli.py tests/test_examples.py tests/test_status_output.py tests/test_evm_integration.py
git commit -m "启用完整多链供应量核对"
```

### Task 9: 验证调用边界、完整回归和部署输出

**Files:**
- Modify: `docs/superpowers/plans/2026-09-10-full-multichain-supply-reconciliation.md`

- [ ] **Step 1: 运行新增功能的完整定向测试**

Run:

```bash
python -m pytest tests/test_supply_assets.py tests/test_multichain_evm.py tests/test_non_evm_supply.py tests/test_multichain_supply.py tests/test_supply_rules.py tests/test_reserve_supply_integration.py tests/test_wechat.py tests/test_config.py tests/test_cli.py tests/test_status_output.py -q
```

Expected: 全部通过，无失败或错误。

- [ ] **Step 2: 运行全量测试**

Run: `python -m pytest -q`

Expected: 全部测试通过；仅保留仓库原有、带明确原因的 skip。

- [ ] **Step 3: 检查新增生产路径没有高成本 RPC 方法**

Run:

```bash
rg -n "eth_getLogs|trace_|debug_|eth_getBlockByNumber|eth_getTransactionReceipt" usd1_monitor/collectors/multichain_evm.py usd1_monitor/collectors/non_evm_supply.py usd1_monitor/collectors/multichain_supply.py
```

Expected: 无输出。

Run:

```bash
rg -n "eth_blockNumber|eth_call|getAccountInfo|triggerconstantcontract|fungible_asset_metadata|current_fungible_asset_balances" usd1_monitor/collectors/multichain_evm.py usd1_monitor/collectors/non_evm_supply.py
```

Expected: 只出现设计批准的当前状态读取方法。

- [ ] **Step 4: 检查格式、范围和提交历史**

Run: `git diff --check origin/main...HEAD`

Expected: 无输出，退出码 0。

Run: `git status --short`

Expected: 仅计划文件的勾选状态尚未提交；没有数据库、日志、密钥、缓存或范围外文件。

Run: `git log --oneline origin/main..HEAD`

Expected: 设计提交以及本计划中列出的中文小步提交。

- [ ] **Step 5: 在本地示例配置上运行离线可执行性检查**

Run: `python -m usd1_monitor --config config.example.yaml status`

Expected: 命令成功，显示新增聚合指标为 UNKNOWN 或已有值，并且 NOT_MONITORED 不包含 `full_multichain_supply_reconciliation`。

网络实况验收由部署环境执行：

```bash
python -m usd1_monitor --config config.yaml check
```

Expected: 输出 6 条原生链、5 条桥接链、5 个桥池、完整原生总量、桥接发行总量、桥池锁仓总量和有符号差额；没有 `eth_getLogs`、trace/debug 请求。

- [ ] **Step 6: 更新计划勾选状态并提交验证记录**

```bash
git add docs/superpowers/plans/2026-09-10-full-multichain-supply-reconciliation.md
git commit -m "记录完整多链供应量核对结果"
```
