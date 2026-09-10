# EVM Permission Monitoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Ethereum 与 BNB Chain 监控改为每 10 分钟执行一次的合约权限、关键状态和日志检查，并停止生产运行时的逐区块完整交易扫描。

**Architecture:** `EvmScanner` 继续按持久化游标扫描 USD1 日志，但使用链配置提供的 500 区块查询跨度和 2,000 区块单轮上限；`EvmSnapshotReader` 在本轮 processed head 读取代理、owner、ProxyAdmin owner、暂停及冻结状态。`EvmChainMonitor` 在同一数据库事务中对账日志、比较快照、更新风险状态和推进游标，生产 builder 不再注入 `PrivilegedCallCollector`。

**Tech Stack:** Python 3.12、asyncio、Pydantic、aiosqlite、eth-utils、pytest/pytest-asyncio

---

### Task 1: 扩展链扫描配置

**Files:**
- Modify: `usd1_monitor/config.py:83-101`
- Modify: `tests/test_config.py`

- [x] **Step 1: 写入会失败的配置测试**

```python
def test_chain_config_accepts_permission_monitor_scan_sizes() -> None:
    chain = ChainConfig(
        chain_id=56,
        rpc_urls=["https://bsc.example.com"],
        confirmation_depth=10,
        interval_seconds=600,
        overlap_blocks=20,
        scan_batch_blocks=2_000,
        log_query_chunk_blocks=500,
    )

    assert chain.scan_batch_blocks == 2_000
    assert chain.log_query_chunk_blocks == 500


def test_chain_config_rejects_log_chunk_larger_than_scan_batch() -> None:
    with pytest.raises(ValueError, match="log_query_chunk_blocks"):
        ChainConfig(
            chain_id=1,
            rpc_urls=["https://eth.example.com"],
            confirmation_depth=3,
            overlap_blocks=20,
            scan_batch_blocks=100,
            log_query_chunk_blocks=101,
        )
```

- [x] **Step 2: 运行测试并确认因字段或上限不存在而失败**

Run: `python -m pytest tests/test_config.py -q`
Expected: FAIL，`scan_batch_blocks=2000` 超出现有上限或 `log_query_chunk_blocks` 被禁止。

- [x] **Step 3: 最小实现配置字段和一致性校验**

```python
class ChainConfig(StrictModel):
    # existing fields unchanged
    scan_batch_blocks: int = Field(default=2_000, gt=0, le=10_000)
    log_query_chunk_blocks: int = Field(default=500, gt=0, le=2_000)

    @model_validator(mode="after")
    def validate_scan_progress(self) -> "ChainConfig":
        if self.scan_batch_blocks <= self.overlap_blocks:
            raise ValueError("scan_batch_blocks must be larger than overlap_blocks")
        if self.log_query_chunk_blocks > self.scan_batch_blocks:
            raise ValueError(
                "log_query_chunk_blocks must not exceed scan_batch_blocks"
            )
        return self
```

- [x] **Step 4: 运行配置测试并确认通过**

Run: `python -m pytest tests/test_config.py -q`
Expected: PASS。

- [x] **Step 5: 提交配置模型改动**

```bash
git add usd1_monitor/config.py tests/test_config.py
git commit -m "支持EVM低频批量扫描配置"
```

### Task 2: 让日志扫描使用配置跨度并能追上 BSC

**Files:**
- Modify: `usd1_monitor/collectors/evm.py:25,75-144`
- Modify: `usd1_monitor/cli.py:94-102`
- Modify: `tests/test_evm_scanner.py`

- [x] **Step 1: 写入 2,000/500 分块和 BSC 追赶测试**

```python
@pytest.mark.asyncio
async def test_scanner_chunks_two_thousand_blocks_into_four_queries(storage) -> None:
    rpc = FakeRpc()
    await storage.set_scan_cursor("bsc", 100)
    rpc.result("eth_blockNumber", hex(2_090))
    for _ in range(4):
        rpc.result("eth_getLogs", [])
    scanner = EvmScanner(
        "bsc", rpc, storage,
        confirmation_depth=10,
        overlap_blocks=20,
        batch_blocks=2_000,
        log_query_chunk_blocks=500,
    )

    result = await scanner.scan_once()

    requests = [params[0] for params in rpc.calls_for("eth_getLogs")]
    assert [(item["fromBlock"], item["toBlock"]) for item in requests] == [
        (hex(81), hex(580)),
        (hex(581), hex(1_080)),
        (hex(1_081), hex(1_580)),
        (hex(1_581), hex(2_080)),
    ]
    assert result.cursor == result.safe_head == 2_080
```

- [x] **Step 2: 运行测试并确认固定 10 区块跨度导致失败**

Run: `python -m pytest tests/test_evm_scanner.py::test_scanner_chunks_two_thousand_blocks_into_four_queries -q`
Expected: FAIL，构造函数不接受 `log_query_chunk_blocks` 或请求次数不是 4。

- [x] **Step 3: 注入日志跨度并从 CLI 传入链配置**

```python
class EvmScanner:
    def __init__(
        self,
        chain: str,
        rpc: RpcClient,
        storage: Storage,
        *,
        confirmation_depth: int,
        overlap_blocks: int,
        batch_blocks: int,
        log_query_chunk_blocks: int = EVM_LOG_QUERY_CHUNK_BLOCKS,
        token_address: str = USD1_TOKEN_ADDRESS,
    ) -> None:
        if log_query_chunk_blocks < 1:
            raise ValueError("log_query_chunk_blocks must be positive")
        self._log_query_chunk_blocks = log_query_chunk_blocks

# scan_once loop
for batch_start in range(start, end + 1, self._log_query_chunk_blocks):
    batch_end = min(end, batch_start + self._log_query_chunk_blocks - 1)

# cli.py EvmScanner construction
log_query_chunk_blocks=chain_config.log_query_chunk_blocks,
```

- [x] **Step 4: 运行 scanner 与 CLI 测试**

Run: `python -m pytest tests/test_evm_scanner.py tests/test_cli.py -q`
Expected: PASS。

- [x] **Step 5: 提交日志批量扫描改动**

```bash
git add usd1_monitor/collectors/evm.py usd1_monitor/cli.py tests/test_evm_scanner.py tests/test_cli.py
git commit -m "降低EVM日志扫描请求数"
```

### Task 3: 解码代理权限标准事件

**Files:**
- Modify: `usd1_monitor/evm_abi.py:35-44,95-172`
- Modify: `tests/test_evm_decode.py`

- [x] **Step 1: 写入 Upgraded 和 AdminChanged 解码测试**

```python
def test_decodes_upgraded_event() -> None:
    implementation = "0x" + "11" * 20
    event = decode_log(
        "ethereum",
        {
            "topics": [
                event_topic("Upgraded(address)"),
                "0x" + "00" * 12 + implementation[2:],
            ],
            "data": "0x",
            "transactionHash": "0xupgrade",
            "logIndex": "0x0",
            "blockNumber": "0x10",
        },
        decimals=18,
    )
    assert event.event_type == "IMPLEMENTATION_CHANGED"
    assert event.to_address == implementation


def test_decodes_admin_changed_event() -> None:
    previous = "0x" + "22" * 20
    current = "0x" + "33" * 20
    event = decode_log(
        "ethereum",
        {
            "topics": [event_topic("AdminChanged(address,address)")],
            "data": (
                "0x" + "00" * 12 + previous[2:]
                + "00" * 12 + current[2:]
            ),
            "transactionHash": "0xadmin",
            "logIndex": "0x1",
            "blockNumber": "0x10",
        },
        decimals=18,
    )
    assert event.event_type == "ADMIN_CHANGED"
    assert event.from_address == previous
    assert event.to_address == current
```

- [x] **Step 2: 运行测试并确认事件当前为 UNKNOWN_LOG**

Run: `python -m pytest tests/test_evm_decode.py -q`
Expected: FAIL，两个事件未登记或未解码。

- [x] **Step 3: 实现标准事件解码及严格长度校验**

```python
EVENTS.update({
    event_topic("Upgraded(address)"): "Upgraded",
    event_topic("AdminChanged(address,address)"): "AdminChanged",
})

def _data_address(data: object, word: int, event_name: str) -> str:
    if not isinstance(data, str) or not data.startswith("0x"):
        raise EvmDecodeError(f"malformed {event_name} data")
    raw = data[2:]
    if len(raw) != 128:
        raise EvmDecodeError(f"malformed {event_name} data")
    return _address("0x" + raw[word * 64:(word + 1) * 64], event_name)

# in decode_log
if event_name == "Upgraded":
    if len(topics) != 2 or data != "0x":
        raise EvmDecodeError("malformed Upgraded log")
    return DecodedEvent(
        chain, "IMPLEMENTATION_CHANGED", tx_hash, log_index, block_number,
        to_address=_address(topics[1], event_name),
    )
if event_name == "AdminChanged":
    if len(topics) != 1:
        raise EvmDecodeError("malformed AdminChanged topics")
    return DecodedEvent(
        chain, "ADMIN_CHANGED", tx_hash, log_index, block_number,
        from_address=_data_address(data, 0, event_name),
        to_address=_data_address(data, 1, event_name),
    )
```

- [x] **Step 4: 运行解码测试并确认通过**

Run: `python -m pytest tests/test_evm_decode.py -q`
Expected: PASS。

- [x] **Step 5: 提交事件解码改动**

```bash
git add usd1_monitor/evm_abi.py tests/test_evm_decode.py
git commit -m "识别代理升级与管理员变更事件"
```

### Task 4: 快照读取 ProxyAdmin owner

**Files:**
- Modify: `usd1_monitor/collectors/evm.py:46-56,193-321`
- Modify: `tests/test_evm_snapshot.py`

- [x] **Step 1: 写入 ProxyAdmin owner 与非 Ownable admin 测试**

```python
@pytest.mark.asyncio
async def test_snapshot_reads_proxy_admin_owner_at_processed_block() -> None:
    rpc = FakeRpc()
    proxy_admin_owner = "0x" + "44" * 20
    rpc.result("eth_getStorageAt", encoded_address(IMPLEMENTATION))
    rpc.result("eth_getStorageAt", encoded_address(ADMIN))
    rpc.result("eth_getCode", "0x60016000")
    rpc.result("eth_call", encoded_address(OWNER))
    rpc.result("eth_call", encoded_address(proxy_admin_owner))
    rpc.result("eth_call", "0x" + "00" * 32)
    snapshot = await EvmSnapshotReader("ethereum", rpc, TOKEN).read(123, NOW)
    assert snapshot.admin_owner == proxy_admin_owner
    observation = next(
        item for item in snapshot.observations
        if item.metric == "evm.admin_owner"
    )
    assert observation.metadata["address"] == proxy_admin_owner
    assert rpc.calls_for("eth_call")[-1][0]["to"] == ADMIN
    assert rpc.calls_for("eth_call")[-1][1] == hex(123)


@pytest.mark.asyncio
async def test_snapshot_treats_empty_admin_owner_result_as_unsupported() -> None:
    rpc = FakeRpc()
    rpc.result("eth_getStorageAt", encoded_address(IMPLEMENTATION))
    rpc.result("eth_getStorageAt", encoded_address(ADMIN))
    rpc.result("eth_getCode", "0x60016000")
    rpc.result("eth_call", encoded_address(OWNER))
    rpc.result("eth_call", "0x")
    rpc.result("eth_call", "0x" + "00" * 32)
    snapshot = await EvmSnapshotReader("ethereum", rpc, TOKEN).read(123, NOW)
    assert snapshot.admin_owner is None
```

- [x] **Step 2: 运行测试并确认快照缺少 admin_owner**

Run: `python -m pytest tests/test_evm_snapshot.py -q`
Expected: FAIL，`EvmSnapshot` 没有 `admin_owner` 或未向 admin 调用 `owner()`。

- [x] **Step 3: 最小实现可选 owner 读取和观察值**

```python
@dataclass(frozen=True)
class EvmSnapshot:
    # existing fields
    admin_owner: str | None = None

async def _optional_owner(self, target: str, block_tag: str) -> str | None:
    try:
        value = await self._rpc.call(
            "eth_call", [{"to": target, "data": OWNER_SELECTOR}, block_tag]
        )
    except RpcResponseError:
        return None
    if value == "0x":
        return None
    decoded = self._decode_storage_address(value, "owner")
    return None if int(decoded[2:], 16) == 0 else decoded

# read()
owner = await self._optional_owner(self._token_address, block_tag)
admin_owner = await self._optional_owner(admin, block_tag)

# append observation
Observation(
    "evm.admin_owner",
    value=1 if admin_owner else 0,
    unit="address",
    metadata={
        "address": admin_owner,
        "supported": admin_owner is not None,
        "block": block_number,
    },
    **common,
)
```

- [x] **Step 4: 运行快照测试并确认通过**

Run: `python -m pytest tests/test_evm_snapshot.py -q`
Expected: PASS。

- [x] **Step 5: 提交 ProxyAdmin owner 快照改动**

```bash
git add usd1_monitor/collectors/evm.py tests/test_evm_snapshot.py
git commit -m "监控ProxyAdmin控制权"
```

### Task 5: 对快照和标准权限事件生成风险状态

**Files:**
- Modify: `usd1_monitor/engine/evm_rules.py:13-17`
- Modify: `usd1_monitor/scheduler.py:750-883`
- Modify: `usd1_monitor/notifications/wechat.py:45-56`
- Modify: `tests/test_evm_rules.py`
- Modify: `tests/test_evm_integration.py`
- Modify: `tests/test_wechat.py`

- [x] **Step 1: 写入 ProxyAdmin owner RED 与事件进入引擎的测试**

```python
def test_proxy_admin_owner_change_is_red() -> None:
    fact = EvmFact(
        "ethereum",
        "ADMIN_OWNER_CHANGED",
        {"previous": ADDRESS_A, "current": ADDRESS_B, "block": 123},
        "snapshot:123:evm.admin_owner",
    )
    assert evaluate_evm_fact(fact, set()).level is RiskLevel.RED


@pytest.mark.asyncio
async def test_monitor_alerts_when_proxy_admin_owner_changes(storage) -> None:
    monitor = EvmChainMonitor(
        "ethereum",
        FakeScanner([scan("ethereum", 100), scan("ethereum", 101)]),
        FakeSnapshotReader([
            snapshot("ethereum", 100, ADDRESS_A, admin_owner=ADDRESS_A),
            snapshot("ethereum", 101, ADDRESS_A, admin_owner=ADDRESS_B),
        ]),
        storage,
    )
    await monitor.check_once(deliver=False)
    result = await monitor.check_once(deliver=False)
    state = await storage.get_risk_state(
        "evm.event.ethereum.snapshot:101:evm.admin_owner"
    )
    assert result.success
    assert state is not None and state.level is RiskLevel.RED
```

- [x] **Step 2: 运行定向测试并确认缺少风险事实**

Run: `python -m pytest tests/test_evm_rules.py tests/test_evm_integration.py -q`
Expected: FAIL，`ADMIN_OWNER_CHANGED` 仍为 GREEN 或未生成快照事实。

- [x] **Step 3: 接入 admin owner 比较及标准事件类型**

```python
RED_IMMUTABLE_FACTS = {
    "IMPLEMENTATION_CHANGED",
    "ADMIN_CHANGED",
    "ADMIN_OWNER_CHANGED",
    "CODE_HASH_CHANGED",
}

# _previous_snapshot_values metrics
"evm.admin_owner",

# _snapshot_facts comparisons
(
    "evm.admin_owner", "address", snapshot.admin_owner,
    "ADMIN_OWNER_CHANGED",
),

# _event_facts relevant set
"IMPLEMENTATION_CHANGED",
"ADMIN_CHANGED",

# notifications/wechat.py FACT_LABELS
"ADMIN_OWNER_CHANGED": "USD1 代理管理员控制人发生变化",
```

对于 `evm.admin_owner`，只有旧观察和新快照都声明 `supported=True` 时才比较；首次建立基线、非 Ownable admin 或接口暂不支持不产生变化事实。

- [x] **Step 4: 运行规则和 EVM 集成测试**

Run: `python -m pytest tests/test_evm_rules.py tests/test_evm_integration.py tests/test_wechat.py -q`
Expected: PASS。

- [x] **Step 5: 提交风险引擎接线**

```bash
git add usd1_monitor/engine/evm_rules.py usd1_monitor/scheduler.py usd1_monitor/notifications/wechat.py tests/test_evm_rules.py tests/test_evm_integration.py tests/test_wechat.py
git commit -m "评估EVM权限快照变化"
```

### Task 6: 停止生产逐区块特权调用扫描

**Files:**
- Modify: `usd1_monitor/cli.py:12-16,103-120`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_evm_integration.py`

- [x] **Step 1: 写入生产 builder 不创建特权扫描器的测试**

```python
def test_builder_does_not_enable_privileged_block_scanner(config, storage) -> None:
    monitor, _ = build_market_monitor(config, storage)
    assert all(
        chain._privileged_collector is None  # noqa: SLF001
        for chain in monitor._evm_chains  # noqa: SLF001
    )
```

并在集成测试的 FakeRpc 调用列表中断言不存在：

```python
assert fake_rpc.calls_for("eth_getBlockByNumber") == []
assert fake_rpc.calls_for("eth_getTransactionReceipt") == []
```

- [x] **Step 2: 运行测试并确认当前 builder 仍注入 collector**

Run: `python -m pytest tests/test_cli.py tests/test_evm_integration.py -q`
Expected: FAIL，生产 EVM monitor 的 `_privileged_collector` 不是 `None`。

- [x] **Step 3: 删除生产 builder 的 collector 导入和注入**

```python
from usd1_monitor.collectors.evm import EvmScanner, EvmSnapshotReader

# EvmChainMonitor(...)
# remove privileged_collector=PrivilegedCallCollector(...)
```

保留 collector 类本身和 `EvmChainMonitor` 的可选兼容路径，避免在本次成本优化中删除公共接口或扩大重构范围。

- [x] **Step 4: 运行 CLI 和 EVM 集成测试**

Run: `python -m pytest tests/test_cli.py tests/test_evm_integration.py -q`
Expected: PASS，且生产构建路径无完整区块和交易回执调用。

- [x] **Step 5: 提交生产接线改动**

```bash
git add usd1_monitor/cli.py tests/test_cli.py tests/test_evm_integration.py
git commit -m "停止生产逐区块权限调用扫描"
```

### Task 7: 更新部署配置、启动文案与说明

**Files:**
- Modify: `config.example.yaml:35-53`
- Modify: `deploy/config.production.example.yaml:28-44`
- Modify: `usd1_monitor/scheduler.py:1250-1262`
- Modify: `tests/test_examples.py`
- Modify: `tests/test_evm_integration.py:900-930`
- Modify: `README.md:18-24`

- [x] **Step 1: 写入示例配置和启动名称测试**

```python
def test_examples_use_ten_minute_permission_monitoring() -> None:
    config = load_config(Path("config.example.yaml"), environ={})
    for chain in (config.chains.ethereum, config.chains.bsc):
        assert chain.interval_seconds == 600
        assert chain.scan_batch_blocks == 2_000
        assert chain.log_query_chunk_blocks == 500

# startup notification assertion
assert "Ethereum 合约权限" in message
assert "BNB Chain 合约权限" in message
assert "链上合约" not in message
```

- [x] **Step 2: 运行测试并确认示例值和文案仍为旧值**

Run: `python -m pytest tests/test_examples.py tests/test_evm_integration.py -q`
Expected: FAIL，周期仍为 30/15 秒且启动名称仍为“链上合约”。

- [x] **Step 3: 更新两份示例和启动名称**

```yaml
interval_seconds: 600
overlap_blocks: 20
scan_batch_blocks: 2000
log_query_chunk_blocks: 500
```

```python
f"{CHAIN_LABELS.get(item.chain, item.chain)} 合约权限"
```

README 增加现有部署升级命令说明：复制示例值到实际 `config.yaml` 后重启服务；程序不会覆盖用户的实际配置文件。

- [x] **Step 4: 运行配置示例、文案与 CLI 测试**

Run: `python -m pytest tests/test_examples.py tests/test_evm_integration.py tests/test_cli.py -q`
Expected: PASS。

- [x] **Step 5: 提交部署和文案改动**

```bash
git add config.example.yaml deploy/config.production.example.yaml usd1_monitor/scheduler.py tests/test_examples.py tests/test_evm_integration.py README.md
git commit -m "更新EVM权限监控部署参数"
```

### Task 8: 全量验证和调用量核对

**Files:**
- Modify: `docs/superpowers/plans/2026-09-10-evm-permission-monitoring.md`

- [x] **Step 1: 运行格式和完整测试套件**

Run: `git diff --check`
Expected: 无输出，退出码 0。

Run: `python -m pytest -q`
Expected: 全部测试通过，无失败或错误。

- [x] **Step 2: 核对生产构建路径的方法集合**

Run: `rg -n "PrivilegedCallCollector|eth_getBlockByNumber|eth_getTransactionReceipt|debug_|trace_" usd1_monitor/cli.py usd1_monitor/scheduler.py`
Expected: `cli.py` 不包含 `PrivilegedCallCollector`；逐区块方法仅可能存在于未被生产 builder 注入的兼容实现，不在默认执行路径。

- [x] **Step 3: 核对配置和工作树范围**

Run: `git status --short`
Expected: 只包含本计划列出的代码、测试、配置、README 和计划勾选修改，不包含多链供应量、网页仪表盘或其他无关改动。

- [x] **Step 4: 更新本计划勾选状态并提交最终验证记录**

```bash
git add docs/superpowers/plans/2026-09-10-evm-permission-monitoring.md
git commit -m "记录EVM权限监控实施结果"
```
