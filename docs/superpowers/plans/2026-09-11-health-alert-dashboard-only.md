# 监控健康异常仅在仪表盘展示实施计划

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 微信只推送影响 USD1 资产安全或稳定性的风险；监控健康异常及其恢复仍写入数据库并显示在网页仪表盘，但不进入微信待发送队列。

**Architecture:** 保留现有采集、规则计算、状态持久化和仪表盘读取链路，只在 `StateEngine.apply_uncommitted()` 构造微信告警分组前过滤 `is_monitoring_health_rule()` 识别出的 transition。这样同一批次中的健康状态仍被持久化和返回，只有资产风险 transition 会继续经过现有分组、格式化、拆分和发送流程。

**Tech Stack:** Python 3.12、asyncio、SQLite、pytest

---

### Task 1: 用回归测试固定通知边界

**Files:**
- Modify: `tests/test_wechat.py:448-525`

**Step 1: 将现有健康告警测试改为“持久化但不入队”**

新增或改写测试 `test_health_transitions_are_persisted_without_wechat_alerts`：

```python
@pytest.mark.asyncio
async def test_health_transitions_are_persisted_without_wechat_alerts(storage) -> None:
    await StateEngine(storage).apply(
        [
            RuleEvaluation("health.por", RiskLevel.RED),
            RuleEvaluation("por.age", RiskLevel.YELLOW),
        ],
        NOW,
    )

    assert await storage.pending_alerts() == []
    states = {state.rule_id: state.level for state in await storage.list_risk_states()}
    assert states["health.por"] is RiskLevel.RED
    assert states["por.age"] is RiskLevel.YELLOW
```

**Step 2: 增加同批健康风险与资产风险的过滤测试**

新增测试 `test_mixed_transitions_only_enqueue_business_alerts`：

```python
@pytest.mark.asyncio
async def test_mixed_transitions_only_enqueue_business_alerts(storage) -> None:
    await StateEngine(storage).apply(
        [
            RuleEvaluation("health.por", RiskLevel.RED),
            RuleEvaluation("market.price", RiskLevel.YELLOW),
        ],
        NOW,
    )

    pending = await storage.pending_alerts()
    assert len(pending) == 1
    assert pending[0].content.startswith("🟡 USD1 注意")
    assert "监控异常" not in pending[0].content
    health = await storage.get_risk_state("health.por")
    assert health is not None
    assert health.level is RiskLevel.RED
```

**Step 3: 增加健康恢复不推送测试**

新增测试 `test_health_recovery_is_persisted_without_wechat_alert`：

```python
@pytest.mark.asyncio
async def test_health_recovery_is_persisted_without_wechat_alert(storage) -> None:
    await storage.set_risk_state("health.por", RiskLevel.RED, NOW, NOW)

    await StateEngine(storage).apply(
        [RuleEvaluation("health.por", RiskLevel.GREEN)], NOW
    )

    assert await storage.pending_alerts() == []
    state = await storage.get_risk_state("health.por")
    assert state is not None
    assert state.level is RiskLevel.GREEN
```

**Step 4: 保留微信超长消息拆分测试的业务覆盖**

把 `test_state_engine_splits_alerts_at_wechat_utf8_limit` 中的规则从 `health.official_wlfi` 改为业务规则 `market.price`，避免健康规则被正确过滤后使该测试失去目标。

**Step 5: 运行测试并确认按预期失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_wechat.py -q`

Expected: 新增的健康静默测试失败，因为当前实现仍会把健康 transition 写入微信待发送队列；既有资产告警测试继续通过。

### Task 2: 在状态引擎入队边界过滤健康状态变化

**Files:**
- Modify: `usd1_monitor/engine/state.py:74-80`
- Test: `tests/test_wechat.py`

**Step 1: 实现最小过滤**

在状态持久化完成、创建 `groups` 前过滤健康 transition：

```python
alertable_transitions = [
    transition
    for transition in transitions
    if not is_monitoring_health_rule(transition.rule_id)
]

groups: dict[str, list[RiskTransition]] = defaultdict(list)
for index, transition in enumerate(alertable_transitions):
    group_key = transition.cause_id or f"rule:{index}:{transition.rule_id}"
    groups[group_key].append(transition)
```

不改动前面的状态写入逻辑，也不改变方法最终返回的完整 `transitions`。`enqueue_alerts=False` 的现有提前返回保持不变。

**Step 2: 运行微信通知测试**

Run: `.venv\Scripts\python.exe -m pytest tests/test_wechat.py -q`

Expected: PASS；健康异常、健康恢复、`por.age` 均不入队，同批资产风险仍正常入队，资产告警拆分行为不变。

**Step 3: 提交实现**

```bash
git add tests/test_wechat.py usd1_monitor/engine/state.py
git commit -m "停止推送监控健康告警"
```

### Task 3: 验证直接影响范围和完整回归

**Files:**
- Verify: `usd1_monitor/engine/state.py`
- Verify: `tests/test_wechat.py`

**Step 1: 运行状态、通知和调度相关测试**

Run: `.venv\Scripts\python.exe -m pytest tests/test_wechat.py tests/test_health.py tests/test_market_integration.py tests/test_reserve_supply_integration.py tests/test_evm_integration.py tests/test_information_integration.py -q`

Expected: PASS；健康状态继续持久化，资产风险通知及市场、储备、EVM、官方信息等直接调用方行为不变。

**Step 2: 运行完整测试集**

Run: `.venv\Scripts\python.exe -m pytest -q`

Expected: 全部通过，允许仓库既有的显式 skip。

**Step 3: 运行静态和依赖健康检查**

Run: `.venv\Scripts\python.exe -m compileall -q usd1_monitor tests`

Expected: exit code 0。

Run: `.venv\Scripts\python.exe -m pip check`

Expected: `No broken requirements found.`

**Step 4: 核对改动范围**

Run: `git diff --check`

Expected: 无输出。

Run: `git status --short`

Expected: 只包含本计划指定的代码和测试改动；不包含配置、数据库、仪表盘、规则阈值或历史告警清理。
