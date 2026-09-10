# USD1 仪表盘审查修复与 PoR 新鲜度 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复仪表盘并发与数据停更误报问题，将 `por.age` 从 USD1 资产风险改为储备数据及时性异常，并让网页及企业微信提示简单、准确、可恢复。

**Architecture:** 在聚合层增加唯一的规则分类函数，所有状态消费者都复用它；PoR 仍保留原规则 ID 和阈值，只拆分“数据恢复”和“数值稳定恢复”的条件；仪表盘继续复用单个只读 SQLite 连接，以实例锁串行化完整快照，并根据采集器最后活动时间合成网页专用停更状态。

**Tech Stack:** Python 3.12、asyncio、aiosqlite、aiohttp、pytest、原生 HTML/CSS/JavaScript。

---

## Task 1：统一资产风险与监控健康分类

**Files:**

- Modify: `usd1_monitor/engine/aggregate.py`
- Modify: `usd1_monitor/engine/state.py`
- Modify: `usd1_monitor/cli.py`
- Test: `tests/test_status_output.py`
- Test: `tests/test_wechat.py`

- [ ] **Step 1: 写入聚合与 CLI 回归测试**

在 `tests/test_status_output.py` 增加 `por.age` 单独为 RED、普通业务规则为 GREEN 的场景：

```python
@pytest.mark.asyncio
async def test_por_age_affects_monitor_health_not_business_overall(
    storage, capsys
) -> None:
    await storage.set_risk_state("market.price", RiskLevel.GREEN, NOW, NOW)
    await storage.set_risk_state("por.age", RiskLevel.RED, NOW, NOW)

    await _print_status(storage)

    output = capsys.readouterr().out
    assert "business_overall: GREEN" in output
    assert "monitor_health: RED" in output
```

同时直接断言 `business_overall()` 排除 `por.age`、`health_overall()` 包含 `por.age`，防止只有 CLI 表面修正。

- [ ] **Step 2: 运行定向测试，确认当前实现失败**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_status_output.py -q
```

Expected: 新测试失败，当前输出仍把 `por.age` 计入 `business_overall`，且 `monitor_health` 没有计入它。

- [ ] **Step 3: 增加共享分类函数并替换前缀判断**

在 `usd1_monitor/engine/aggregate.py` 增加：

```python
def is_monitoring_health_rule(rule_id: str) -> bool:
    return rule_id.startswith("health.") or rule_id == "por.age"
```

将聚合函数改为：

```python
if not is_monitoring_health_rule(state.rule_id)
```

`business_overall()` 使用反向条件，`health_overall()` 使用正向条件。然后在 `usd1_monitor/cli.py` 和 `usd1_monitor/engine/state.py` 导入并复用该函数，替换各自的 `startswith("health.")` 分类。

状态引擎分组选择总体等级时使用：

```python
if all(is_monitoring_health_rule(item.rule_id) for item in group)
```

- [ ] **Step 4: 运行分类与通知状态测试**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_status_output.py tests/test_wechat.py -q
```

Expected: PASS；既有业务告警、`health.*` 告警和失败通知总体等级行为不变。

- [ ] **Step 5: 提交本任务**

```powershell
git add usd1_monitor/engine/aggregate.py usd1_monitor/engine/state.py usd1_monitor/cli.py tests/test_status_output.py tests/test_wechat.py
git commit -m "统一储备新鲜度状态分类"
```

## Task 2：修正 PoR 更新延迟文案与恢复条件

**Files:**

- Modify: `usd1_monitor/notifications/wechat.py`
- Modify: `usd1_monitor/scheduler.py`
- Modify: `tests/test_wechat.py`
- Modify: `tests/test_reserve_supply_integration.py`

- [ ] **Step 1: 写入企业微信专用文案测试**

在 `tests/test_wechat.py` 增加延迟和恢复两个场景。延迟场景使用 `current=128.58028327 * 60`，断言：

```python
assert content.startswith("🔴 USD1 储备数据更新延迟")
assert "发生了什么：官方储备数据已有约 129 分钟未更新" in content
assert "说明：这不代表储备不足，只表示目前无法获得最新储备信息。" in content
assert "USD1 危险" not in content
assert "128.58028327" not in content
```

恢复场景断言标题为 `🟢 USD1 储备数据已恢复更新`。再增加一个场景：即使传入的监控总体等级为 RED，单个 `por.age` 的 YELLOW 转换仍显示黄色延迟标题，避免被其他健康故障误染成红色。

- [ ] **Step 2: 把原“两次读取恢复”测试改成“一条新鲜记录恢复”**

将 `tests/test_reserve_supply_integration.py` 中：

```python
test_por_red_state_needs_two_fresh_reads_to_recover
```

改为 `test_por_age_recovers_after_one_fresh_bundle`，保留单条新鲜 observation，期望 `por.age` 变为 GREEN。不要修改已有三组 `por.reserve_change` 稳定恢复测试。

- [ ] **Step 3: 运行新测试，确认文案和恢复条件均失败**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_wechat.py tests/test_reserve_supply_integration.py -q
```

Expected: 新增的专用标题、整数分钟、解释文本及单条恢复断言失败。

- [ ] **Step 4: 实现 `por.age` 专用通知格式**

在 `format_transitions()` 中识别全部为 `por.age` 的组：

```python
freshness_only = all(item.rule_id == "por.age" for item in items)
health_only = all(is_monitoring_health_rule(item.rule_id) for item in items)
display_level = transition_level if freshness_only else max(overall, transition_level)
```

专用标题规则：

```python
if freshness_only:
    heading = (
        "🟢 USD1 储备数据已恢复更新"
        if recovered
        else f"{'🟡' if display_level is RiskLevel.YELLOW else '🔴'} "
             "USD1 储备数据更新延迟"
    )
```

在 `_human_summary()` 的 `por.age` 分支中，将未恢复摘要改为 `官方储备数据已有约 N 分钟未更新`，正数分钟按 `int(age / 60 + 0.5)` 四舍五入；未恢复时在 details 中加入：

```python
"说明：这不代表储备不足，只表示目前无法获得最新储备信息。"
```

保留原信息来源和北京时间。为 `por.age` 使用“请稍后查看官方储备页面是否恢复更新”的专用建议；恢复消息沿用“继续观察一段时间”。

- [ ] **Step 5: 只解除 `por.age` 的双记录恢复限制**

在 `usd1_monitor/scheduler.py::_por_evaluations()` 删除读取 `prior_age` 并依赖 `result.valid_recovery` 保持旧级别的代码，直接使用：

```python
age_level = result.age_level
```

保留 `distinct_collections`、`stable_recovery` 和 `por.reserve_change` 分支，确保储备数值大幅变化仍需配置数量的不同稳定 bundle 才能恢复。

- [ ] **Step 6: 运行 PoR 与通知回归测试**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_wechat.py tests/test_reserve_supply_integration.py -q
```

Expected: PASS；`por.age` 一条新鲜数据恢复，`por.reserve_change` 的重复 bundle、两条稳定 bundle、持续变化测试仍全部通过。

- [ ] **Step 7: 提交本任务**

```powershell
git add usd1_monitor/notifications/wechat.py usd1_monitor/scheduler.py tests/test_wechat.py tests/test_reserve_supply_integration.py
git commit -m "修正储备更新延迟提示与恢复"
```

## Task 3：让仪表盘快照并发安全并识别监控停更

**Files:**

- Modify: `usd1_monitor/dashboard_data.py`
- Modify: `tests/test_dashboard_data.py`
- Modify: `tests/test_dashboard_server.py`

- [ ] **Step 1: 写入并发、回滚和停更测试**

在 `tests/test_dashboard_data.py` 增加：

```python
snapshots = await asyncio.gather(
    *(repository.snapshot(now=NOW) for _ in range(10))
)
assert len(snapshots) == 10
```

再通过 monkeypatch 让一次 `_metrics()` 抛出 `RuntimeError`，断言异常向上传播，并在恢复原方法后下一次 `snapshot()` 成功，证明非预期异常也已回滚事务。

新增采集器最后成功时间为 `NOW` 的测试：

- `NOW + timedelta(minutes=14, seconds=59)` 不生成 `health.monitor_stale`；
- `NOW + timedelta(minutes=15)` 时 health 为 RED，items 包含 `health.monitor_stale`，摘要为“监控数据已经停止更新”；
- 完全没有采集器活动且没有健康状态时继续为 UNKNOWN。

将已有 `por.age` 仪表盘测试调整为从 `snapshot["health"]["items"]` 读取，并断言它不出现在 business items。

- [ ] **Step 2: 运行仪表盘数据测试，确认当前实现失败**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_dashboard_data.py tests/test_dashboard_server.py -q
```

Expected: 并发读取出现 `cannot start a transaction within a transaction` 对应的 `DashboardDataError`；停更仍显示旧状态；`por.age` 仍位于 business。

- [ ] **Step 3: 为完整快照增加实例级锁和可靠回滚**

在 `DashboardRepository.__init__()` 创建：

```python
self._snapshot_lock = asyncio.Lock()
```

把 `BEGIN` 到 `COMMIT` 的完整事务放在：

```python
async with self._snapshot_lock:
```

保留 SQLite/数据解析错误向 `DashboardDataError` 的转换，并增加：

```python
except BaseException:
    await connection.rollback()
    raise
```

不新增连接、不改只读 URI 和 `PRAGMA query_only=ON`。

- [ ] **Step 4: 统一仪表盘分类并计算最后监控活动**

在 `_state_group()` 使用 `is_monitoring_health_rule()` 选择 health/business。让 `_collector_health()` 在同一次查询结果中同时返回前端 collector 列表和最后活动时间；最后活动是每行 `last_success_at`、`last_failure_at` 所有非空值的最大值。

定义固定常量：

```python
MONITOR_STALE_SECONDS = 900
```

当存在最后活动且 `now - last_activity >= 900 秒` 时，将 health 设为 RED，并添加网页专用 item：

```python
{
    "rule_id": "health.monitor_stale",
    "level": "RED",
    "summary": "监控数据已经停止更新",
    "first_triggered_at": self._format_time(
        last_activity + timedelta(seconds=MONITOR_STALE_SECONDS)
    ),
    "changed_at": self._format_time(
        last_activity + timedelta(seconds=MONITOR_STALE_SECONDS)
    ),
}
```

在 `RULE_LABELS` 增加该规则的中文兜底标签。没有任何活动时间时不合成停更项，保持冷启动 UNKNOWN。

- [ ] **Step 5: 运行仪表盘后端与只读集成测试**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_dashboard_data.py tests/test_dashboard_server.py -q
```

Expected: PASS；10 个并发快照均成功，异常后可再次读取，15 分钟边界准确，HTTP 读取前后数据库 SHA-256 不变。

- [ ] **Step 6: 提交本任务**

```powershell
git add usd1_monitor/dashboard_data.py tests/test_dashboard_data.py tests/test_dashboard_server.py
git commit -m "修复仪表盘并发与停更识别"
```

## Task 4：修正网页百分比与提醒分组

**Files:**

- Modify: `usd1_monitor/dashboard_static/dashboard.js`
- Modify: `usd1_monitor/dashboard_static/dashboard.css`
- Modify: `tests/test_dashboard_assets.py`

- [ ] **Step 1: 写入前端静态验收断言**

在 `tests/test_dashboard_assets.py` 断言 JavaScript 明确区分覆盖率和变化率，并含两个可见分组标题：

```python
assert '["estimated_collateralization", "估算储备覆盖率", "ratio"]' in javascript
assert '["supply_change_24h", "24 小时供应量变化", "change-percent"]' in javascript
assert '"资产风险"' in javascript
assert '"数据与监控异常"' in javascript
assert ".risk-group" in css
```

- [ ] **Step 2: 运行静态资源测试，确认当前实现失败**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_dashboard_assets.py -q
```

Expected: 新断言失败，因为两个百分比指标当前共用带正号格式，提醒也未分组。

- [ ] **Step 3: 分开格式化比率与变化率**

把 METRICS 类型改为：

```javascript
["estimated_collateralization", "估算储备覆盖率", "ratio"],
["supply_change_24h", "24 小时供应量变化", "change-percent"],
```

`formatMetric()` 中：

- `ratio`：若 unit 为 `ratio` 则乘 100，最多两位小数，不显示正号；
- `change-percent`：直接使用百分数值，最多两位小数，使用 `signDisplay: "exceptZero"`。

验收示例：覆盖率 `1.0238 ratio` 显示 `102.38%`；变化率 `5 percent` 显示 `+5%`。

- [ ] **Step 4: 将当前提醒按语义分组渲染**

保留 `#active-risk-list` 外层容器。提取渲染单个 item 的小函数，在 `renderActiveRisks()` 中分别处理：

```javascript
const groups = [
  ["资产风险", snapshot.business?.items || []],
  ["数据与监控异常", snapshot.health?.items || []],
];
```

仅渲染非空分组；两个数组都为空时继续显示现有正常/UNKNOWN 文案。使用 `textContent` 和现有 `element()`，不引入 `innerHTML`。

在 CSS 中给 `.risk-group` 和 `.risk-group-title` 增加最小布局样式，保持 `.risk-item > div { min-width: 0; }` 和 `overflow-wrap: anywhere`，避免手机端长文本横向溢出。

- [ ] **Step 5: 运行前端与 API 回归测试**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_dashboard_assets.py tests/test_dashboard_data.py tests/test_dashboard_server.py -q
```

Expected: PASS；页面仍无外部依赖，API 格式未变，响应式约束仍存在。

- [ ] **Step 6: 提交本任务**

```powershell
git add usd1_monitor/dashboard_static/dashboard.js usd1_monitor/dashboard_static/dashboard.css tests/test_dashboard_assets.py
git commit -m "优化仪表盘提醒与百分比展示"
```

## Task 5：全量验证与范围核对

**Files:**

- Verify only; production changes are not expected in this task.

- [ ] **Step 1: 运行全部自动化测试**

Run:

```powershell
.venv\Scripts\python.exe -m pytest -q
```

Expected: 全部 PASS，无 skipped 之外的新失败。

- [ ] **Step 2: 运行语法、依赖和补丁检查**

Run:

```powershell
.venv\Scripts\python.exe -m compileall -q usd1_monitor tests
.venv\Scripts\python.exe -m pip check
git diff --check
```

Expected: 三条命令退出码均为 0；`pip check` 输出 `No broken requirements found.`；无空白错误。

- [ ] **Step 3: 再次验证仪表盘只读与并发 API**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_dashboard_server.py::test_dashboard_http_reads_existing_database_without_mutation tests/test_dashboard_data.py -q
```

Expected: PASS；仪表盘请求前后数据库 SHA-256 一致，连续及并发快照都成功。此步骤不运行 `check`，避免验收过程写入真实监控数据库或调用外部 RPC。

- [ ] **Step 4: 在桌面与手机宽度做页面视觉验收**

Run:

```powershell
.venv\Scripts\python.exe -m usd1_monitor --config config.example.yaml dashboard
```

在浏览器打开 `http://127.0.0.1:8080`，分别使用 `1280×900` 和 `390×844` 视口检查：

- 储备覆盖率无 `+`，24 小时正变化有 `+`；
- “资产风险”和“数据与监控异常”仅在各自有内容时显示；
- 长摘要、时间和来源不产生横向滚动；
- 停止服务 15 分钟的逻辑由自动化时间推进测试验证，不在这里真实等待。

Expected: 两种视口布局完整、无横向溢出，控制台无 JavaScript 错误。完成后用 `Ctrl+C` 停止只读 dashboard 进程。

- [ ] **Step 5: 核对最终 diff 没有扩大范围**

Run:

```powershell
git status --short
git diff --stat HEAD~4..HEAD
git diff HEAD~4..HEAD -- usd1_monitor tests
```

Expected: 只包含本计划列出的 Python、测试和 dashboard 静态资源；无数据库迁移、依赖升级、配置项新增、采集频率改变或无关格式化。

- [ ] **Step 6: 汇总验证证据，不自动推送**

向用户报告：修复点、测试数量和结果、当前分支/提交；除非用户明确要求，不执行 `git push` 或更新 PR。
