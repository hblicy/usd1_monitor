# USD1 Monitor Postfix Review Fixes Implementation Plan

**Goal:** 修复最终复审确认的覆盖率错配、重组状态残留、孤块告警复活、旧告警兼容和限流额度空耗。

**Architecture:** 保留单进程 asyncio 与现有 SQLite schema。覆盖率通过显式的“本轮 PoR 已成功”信号控制；可变 EVM 状态在 block-hash 重组时由当前安全快照强制重算；告警取消与限流 claim 均在现有 SQLite 写锁和事务内完成。

**Tech Stack:** Python 3.11+、asyncio、aiosqlite、pytest。

---

### Task 1: 覆盖率使用本轮成功输入

**Files:**
- Modify: `usd1_monitor/scheduler.py`
- Test: `tests/test_reserve_supply_integration.py`

- [x] 增加回归测试：同轮 PoR 失败、global supply 成功时不得用旧 PoR 创建覆盖率。
- [x] 增加回归测试：同轮 PoR/global supply 均成功时，即使存在未满一小时的旧覆盖率，也必须用本轮两项输入更新。
- [x] 运行两个测试并确认因当前隐式 `_coverage_update()` 行为失败。
- [x] 给 supply 持久化路径增加显式 `update_coverage` / `force_coverage` 控制；同时到期时仅在本轮 PoR 成功后计算，并绕过旧 ratio 周期门控。
- [x] 运行 `tests/test_reserve_supply_integration.py` 至通过。

### Task 2: 重组强制恢复 paused 状态

**Files:**
- Modify: `usd1_monitor/scheduler.py`
- Test: `tests/test_evm_integration.py`

- [x] 增加三轮回归测试：canonical false → orphan true → reorg canonical false，最终 `evm.<chain>.paused` 必须为 GREEN。
- [x] 运行测试并确认当前值仍为 RED。
- [x] block-hash reorg 时强制根据当前安全快照评估 paused；保持普通轮次的差异检测不变。
- [x] 运行 EVM 集成测试至通过。

### Task 3: 完整取消新旧孤块告警

**Files:**
- Modify: `usd1_monitor/storage.py`
- Test: `tests/test_storage.py`

- [x] 增加回归测试：snapshot 告警处于 IN_FLIGHT 时发生重组，晚到失败结果不得将其复活为 PENDING。
- [x] 增加回归测试：HEAD 旧版 `snapshot:<block>:paused:*` 告警可从正文规则名识别链并取消，另一链同高度记录保留。
- [x] 运行测试并确认当前状态分别错误为 PENDING 和未取消。
- [x] 取消查询纳入 IN_FLIGHT；旧格式兼容精确识别 `evm.<chain>.paused` 和 `evm.<chain>.freeze.`。
- [x] 运行 storage 与 EVM 集成测试至通过。

### Task 4: 原子 claim 与限流预算

**Files:**
- Modify: `usd1_monitor/storage.py`
- Modify: `usd1_monitor/scheduler.py`
- Test: `tests/test_wechat.py`

- [x] 增加回归测试：pending 快照读取后被取消，不得消耗唯一发送额度，下一条真实告警仍可发送。
- [x] 运行测试并确认第二条告警因额度空耗未发送。
- [x] 在单个 SQLite 事务内先 claim 告警，再获取持久化额度；额度不足时同事务恢复 PENDING，claim 失败时不消耗额度。
- [x] 保留启动消息的独立持久化 `try_acquire()` 路径。
- [x] 运行通知与 storage 测试至通过。

### Task 5: 完整验证

- [x] 运行 EVM、storage、通知、储备供应定向测试。
- [x] 运行全量离线测试。
- [x] 运行 `compileall`、`pip check` 和 `git diff --check`。
- [x] 不创建提交、不覆盖或整理工作区内其他已有修改。
