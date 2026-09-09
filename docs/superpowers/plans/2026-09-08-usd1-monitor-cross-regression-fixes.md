# USD1 Monitor Cross-Regression Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复强制覆盖率刷新制造伪连续小时点，以及同高度 snapshot-only 告警在跨链重组时被误取消的问题。

**Architecture:** 覆盖率仍沿用现有一小时滚动周期；强制刷新若落在最新点的一小时内，只在同一事务中替换最新点，再用剩余的真实小时点评估。snapshot-only 告警的 cause 新格式加入链名；回滚逻辑继续兼容旧 cause，但通过告警正文中的精确规则名前缀限定链。

**Tech Stack:** Python 3.11+、asyncio、aiosqlite、pytest。

---

### Task 1: 强制覆盖率刷新替换周期内旧点

**Files:**
- Modify: `tests/test_reserve_supply_integration.py`
- Modify: `usd1_monitor/storage.py`
- Modify: `usd1_monitor/scheduler.py`

- [x] **Step 1: 写入失败回归测试**

  在 `test_same_cycle_inputs_replace_recent_coverage_point` 中断言覆盖率历史只保留当前点，证明 30 分钟旧点不会和当前点组成“连续两小时”。

- [x] **Step 2: 运行测试并确认 RED**

  Run: `pytest -q tests/test_reserve_supply_integration.py::test_same_cycle_inputs_replace_recent_coverage_point`

  Expected: FAIL，历史中当前会同时存在 recent/current 两个点。

- [x] **Step 3: 实现最小修复**

  在 `Storage` 增加仅供事务内使用的删除方法：

  ```python
  async def delete_latest_observation_uncommitted(
      self, metric: str, scope: str
  ) -> None:
      await self.connection.execute(
          """
          DELETE FROM observations
          WHERE id = (
              SELECT id FROM observations
              WHERE metric = ? AND scope = ?
              ORDER BY observed_at DESC, id DESC
              LIMIT 1
          )
          """,
          (metric, scope),
      )
  ```

  `_coverage_update()` 仅在 `force=True` 且最新点距当前小于配置周期时，在插入新点前调用该方法。输入缺失或过期时不得删除旧点。

- [x] **Step 4: 运行测试并确认 GREEN**

  Run: `pytest -q tests/test_reserve_supply_integration.py`

  Expected: PASS。

### Task 2: snapshot-only cause 按链隔离

**Files:**
- Modify: `tests/test_storage.py`
- Modify: `tests/test_evm_integration.py`
- Modify: `usd1_monitor/scheduler.py`
- Modify: `usd1_monitor/storage.py`

- [x] **Step 1: 写入失败回归测试**

  增加测试确认 snapshot-only implementation 变化的 cause 为 `snapshot:<chain>:<block>:evm.implementation`；再构造 Ethereum/BSC 同高度旧格式告警，回滚 Ethereum 后只取消 Ethereum 告警。

- [x] **Step 2: 运行测试并确认 RED**

  Run: `pytest -q tests/test_evm_integration.py tests/test_storage.py`

  Expected: 新 cause 缺少链名，或 BSC 同高度告警被误取消。

- [x] **Step 3: 实现最小修复**

  `_snapshot_facts()` 在没有链上事件 cause 时使用：

  ```python
  cause_id = event_cause or (
      f"snapshot:{self.chain}:{snapshot.block_number}:{metric}"
  )
  ```

  `cancel_reorged_snapshot_alerts_uncommitted()` 识别 paused、frozen 与四种 immutable metric；旧格式仅在正文包含 `evm.event.<chain>.snapshot:` 或已有的精确链规则标识时匹配。`rollback_evm_chain_from_uncommitted()` 删除状态后不再调用无链名的通用 cause 取消函数。

- [x] **Step 4: 运行测试并确认 GREEN**

  Run: `pytest -q tests/test_evm_integration.py tests/test_storage.py`

  Expected: PASS。

### Task 3: 完整验证

- [x] 运行覆盖率、EVM、storage 定向测试。
- [x] 运行全量离线测试。
- [x] 运行 `python -m compileall -q usd1_monitor tests`、`python -m pip check`、`git diff --check`。
- [x] 不创建提交，不整理或覆盖工作区其他已有修改。
