# Aptos 供应量与仪表盘数据恢复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 Aptos 供应量和零桥池余额解析，使完整多链指标恢复，并让仪表盘隐藏旧采集器状态、说明暂不可计算指标的原因。

**Architecture:** 保留现有单次 Aptos GraphQL 请求和完整多链聚合规则，只在 Aptos 响应边界规范化无损整数并解释精确查询的空余额。旧健康状态仅在仪表盘读取层过滤；缺失指标原因由前端依据现有快照值和时间推导，不修改数据库与接口结构。

**Tech Stack:** Python 3.12、pytest、asyncio、SQLite、原生 JavaScript。

---

### Task 1: 修复 Aptos 整数供应量解析

**Files:**
- Modify: `tests/test_non_evm_supply.py`
- Modify: `usd1_monitor/collectors/non_evm_supply.py`

- [ ] **Step 1: 写入整数供应量回归测试**

在 `tests/test_non_evm_supply.py` 增加：

```python
@pytest.mark.asyncio
async def test_aptos_accepts_integer_supply_v2() -> None:
    http = FakeHttp()
    url = "https://aptos.example/v1/graphql"
    body = aptos_response(
        supply="4200000000000", balance="1250000000000"
    )
    body["data"]["fungible_asset_metadata"][0]["supply_v2"] = 16211958179163
    http.queue_json(url, body, method="POST")

    batch = await AptosSupplyCollector(http, [url]).collect(NOW)

    assert batch.errors == ()
    assert batch.snapshots[0].supply == pytest.approx(16_211_958.179163)
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `python -m pytest tests/test_non_evm_supply.py::test_aptos_accepts_integer_supply_v2 -q`

Expected: FAIL，`batch.errors` 包含 `Aptos USD1 supply_v2 is malformed`。

- [ ] **Step 3: 增加严格的无符号整数规范化函数并应用到供应量**

在 `usd1_monitor/collectors/non_evm_supply.py` 增加：

```python
def _parse_uint(value: object, error: str) -> int:
    if isinstance(value, bool):
        raise SupplyDataError(error)
    if isinstance(value, int):
        if value < 0:
            raise SupplyDataError(error)
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise SupplyDataError(error)
```

将 `supply_v2` 的字符串专用校验替换为：

```python
raw_supply = _parse_uint(
    row.get("supply_v2"),
    "Aptos USD1 supply_v2 is malformed",
)
```

传给 `_snapshot` 时直接使用 `raw_supply`。

- [ ] **Step 4: 运行整数与现有非法值测试并确认 GREEN**

Run: `python -m pytest tests/test_non_evm_supply.py -q`

Expected: PASS，整数和数字字符串均成功，空值、负数等现有非法输入仍失败。

- [ ] **Step 5: 增加布尔值拒绝测试**

增加参数化测试，确保 JSON 布尔值不会被 Python 当作整数：

```python
@pytest.mark.asyncio
async def test_aptos_rejects_boolean_supply_v2() -> None:
    http = FakeHttp()
    url = "https://aptos.example/v1/graphql"
    body = aptos_response(supply="1", balance="0")
    body["data"]["fungible_asset_metadata"][0]["supply_v2"] = True
    http.queue_json(url, body, method="POST")

    batch = await AptosSupplyCollector(http, [url]).collect(NOW)

    assert [item[0] for item in batch.errors] == ["native_aptos"]
```

- [ ] **Step 6: 运行测试并提交**

Run: `python -m pytest tests/test_non_evm_supply.py -q`

Expected: PASS。

Commit:

```bash
git add tests/test_non_evm_supply.py usd1_monitor/collectors/non_evm_supply.py
git commit -m "修复 Aptos 供应量整数解析"
```

### Task 2: 将 Aptos 精确空余额解释为零

**Files:**
- Modify: `tests/test_non_evm_supply.py`
- Modify: `usd1_monitor/collectors/non_evm_supply.py`

- [ ] **Step 1: 写入空桥池余额回归测试**

```python
@pytest.mark.asyncio
async def test_aptos_empty_pool_rows_mean_zero_balance() -> None:
    http = FakeHttp()
    url = "https://aptos.example/v1/graphql"
    body = aptos_response(
        supply="16211958179163", balance="1"
    )
    body["data"]["current_fungible_asset_balances"] = []
    http.queue_json(url, body, method="POST")

    batch = await AptosSupplyCollector(http, [url]).collect(NOW)

    assert batch.errors == ()
    assert [item.supply for item in batch.snapshots] == [
        pytest.approx(16_211_958.179163),
        0,
    ]
    assert batch.snapshots[1].observation.metadata["component_id"] == "locked_aptos"
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `python -m pytest tests/test_non_evm_supply.py::test_aptos_empty_pool_rows_mean_zero_balance -q`

Expected: FAIL，返回 `Aptos USD1 pool row must be unique`。

- [ ] **Step 3: 实现空结果为零且保留重复行校验**

将桥池列表处理改为：

```python
if not isinstance(balances, list):
    raise SupplyDataError("Aptos USD1 pool row must be unique")
if not balances:
    raw_balance = 0
elif len(balances) != 1:
    raise SupplyDataError("Aptos USD1 pool row must be unique")
else:
    row = balances[0]
    if (
        not isinstance(row, dict)
        or row.get("asset_type") != APTOS_METADATA
        or row.get("owner_address") != APTOS_POOL
    ):
        raise SupplyDataError("Aptos USD1 pool identity mismatch")
    raw_balance = _parse_uint(
        row.get("amount"),
        "Aptos USD1 pool amount is malformed",
    )
```

继续使用现有 `_snapshot` 生成 `locked_aptos`，不要增加第二次请求。

- [ ] **Step 4: 运行 Aptos 采集器测试并确认 GREEN**

Run: `python -m pytest tests/test_non_evm_supply.py -q`

Expected: PASS，包括既有重复行、身份不匹配和负数测试。

- [ ] **Step 5: 运行完整多链聚合测试**

Run: `python -m pytest tests/test_multichain_supply.py tests/test_reserve_supply_integration.py -q`

Expected: PASS，现有完整性约束和四项汇总计算不变。

- [ ] **Step 6: 提交修复**

```bash
git add tests/test_non_evm_supply.py usd1_monitor/collectors/non_evm_supply.py
git commit -m "支持 Aptos 桥池零余额"
```

### Task 3: 隐藏旧版 ETH/BSC 健康状态

**Files:**
- Modify: `tests/test_dashboard_data.py`
- Modify: `usd1_monitor/dashboard_data.py`

- [ ] **Step 1: 写入旧状态过滤回归测试**

```python
@pytest.mark.asyncio
async def test_snapshot_ignores_legacy_supply_health_rows(storage) -> None:
    for collector_id in ("supply_ethereum", "supply_bsc"):
        await storage.record_collector_failure(collector_id, NOW, "old 429")
        await storage.set_risk_state(
            f"health.{collector_id}", RiskLevel.RED, NOW, NOW
        )
    await storage.record_collector_success("supply_native_ethereum", NOW)
    await storage.record_collector_success("supply_native_bsc", NOW)
    repository = DashboardRepository(storage.path)
    await repository.open()
    try:
        health = (await repository.snapshot(now=NOW))["health"]
    finally:
        await repository.close()

    assert health["level"] == "UNKNOWN"
    assert health["items"] == []
    assert {item["collector_id"] for item in health["collectors"]} == {
        "supply_native_ethereum",
        "supply_native_bsc",
    }
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `python -m pytest tests/test_dashboard_data.py::test_snapshot_ignores_legacy_supply_health_rows -q`

Expected: FAIL，旧规则仍把健康状态置为 RED，旧 collector 仍出现在列表中。

- [ ] **Step 3: 在仪表盘读取层过滤旧 ID**

在 `usd1_monitor/dashboard_data.py` 增加：

```python
LEGACY_COLLECTOR_IDS = frozenset({"supply_ethereum", "supply_bsc"})
LEGACY_HEALTH_RULE_IDS = frozenset(
    f"health.{collector_id}" for collector_id in LEGACY_COLLECTOR_IDS
)
```

`_risk_states` 构造结果时排除 `LEGACY_HEALTH_RULE_IDS`；`_collector_health` 循环开始处排除 `LEGACY_COLLECTOR_IDS`，并且不要让旧记录参与 `last_activity` 计算。

- [ ] **Step 4: 运行仪表盘数据测试并确认 GREEN**

Run: `python -m pytest tests/test_dashboard_data.py -q`

Expected: PASS，当前 collector 的健康状态仍正常展示。

- [ ] **Step 5: 提交修复**

```bash
git add tests/test_dashboard_data.py usd1_monitor/dashboard_data.py
git commit -m "忽略旧版供应量健康状态"
```

### Task 4: 为不可计算指标显示具体原因

**Files:**
- Modify: `tests/test_dashboard_assets.py`
- Modify: `usd1_monitor/dashboard_static/dashboard.js`

- [ ] **Step 1: 写入前端资源约束测试**

在 `tests/test_dashboard_assets.py` 增加：

```python
def test_dashboard_assets_explain_unavailable_supply_metrics() -> None:
    javascript = (ASSET_ROOT / "dashboard.js").read_text(encoding="utf-8")

    assert "function unavailableMetricReason" in javascript
    assert "等待完整多链供应量采集" in javascript
    assert "等待官方储备数据更新" in javascript
    assert "正在积累24小时完整数据" in javascript
    assert "renderMetrics(snapshot.metrics, snapshot.generated_at)" in javascript
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `python -m pytest tests/test_dashboard_assets.py::test_dashboard_assets_explain_unavailable_supply_metrics -q`

Expected: FAIL，前端尚无缺失原因函数。

- [ ] **Step 3: 实现缺失原因推导**

在 `dashboard.js` 增加：

```javascript
const METRIC_FRESH_MS = 4500 * 1000;

function unavailableMetricReason(key, metrics, generatedAt) {
  const completeSupply = metrics?.multichain_supply || null;
  if (["multichain_supply", "bridged_total", "locked_total", "bridge_delta"].includes(key)) {
    return "等待完整多链供应量采集";
  }
  if (key === "estimated_collateralization") {
    if (!completeSupply) return "等待完整多链供应量采集";
    const reserves = metrics?.reserves || null;
    if (!reserves) return "等待官方储备数据更新";
    const generated = Date.parse(generatedAt);
    const observed = Date.parse(reserves.observed_at);
    if (
      Number.isNaN(generated)
      || Number.isNaN(observed)
      || generated - observed > METRIC_FRESH_MS
    ) {
      return "等待官方储备数据更新";
    }
    return "等待下一次覆盖率计算";
  }
  if (key === "supply_change_24h") {
    return completeSupply
      ? "正在积累24小时完整数据"
      : "等待完整多链供应量采集";
  }
  return "暂无数据";
}
```

将 `renderMetrics` 改为接收 `generatedAt`。指标无值时，主值显示 `unavailableMetricReason(key, metrics, generatedAt)`，说明行显示 `暂时无法计算`；有值时继续显示原有数值和时间。`renderSnapshot` 传入 `snapshot.generated_at`，不可用页面传入 `null`。

- [ ] **Step 4: 运行静态资源测试与语法检查**

Run: `python -m pytest tests/test_dashboard_assets.py -q`

Expected: PASS。

Run: `node --check usd1_monitor/dashboard_static/dashboard.js`

Expected: exit 0，无语法错误。

- [ ] **Step 5: 提交前端改动**

```bash
git add tests/test_dashboard_assets.py usd1_monitor/dashboard_static/dashboard.js
git commit -m "说明仪表盘指标暂缺原因"
```

### Task 5: 完整验证与范围审查

**Files:**
- Verify only

- [ ] **Step 1: 运行直接相关测试**

Run: `python -m pytest tests/test_non_evm_supply.py tests/test_multichain_supply.py tests/test_reserve_supply_integration.py tests/test_dashboard_data.py tests/test_dashboard_assets.py -q`

Expected: PASS。

- [ ] **Step 2: 运行完整测试集**

Run: `python -m pytest -q`

Expected: 所有测试通过，仅保留项目已有 skip。

- [ ] **Step 3: 检查 Python 编译、依赖与差异**

Run: `python -m compileall -q usd1_monitor tests`

Expected: exit 0。

Run: `python -m pip check`

Expected: `No broken requirements found.`

Run: `git diff --check`

Expected: 无输出。

- [ ] **Step 4: 审查最终变更范围**

Run: `git status --short`

Expected: 无未提交文件。

Run: `git diff HEAD~3..HEAD --stat`

Expected: 仅包含 Aptos 采集器、相关测试、仪表盘读取/静态资源及其测试；不包含数据库迁移、RPC 配置或无关重构。
