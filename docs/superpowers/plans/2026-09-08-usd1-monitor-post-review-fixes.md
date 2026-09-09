# USD1 Monitor Post-review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复最终复审确认的 EVM 冷启动/追赶/误报、独立调度、outbox 重组竞态、官方鉴证选择和市场告警证据缺口。

**Architecture:** 保留单进程 asyncio + SQLite。EVM 在安全区块直接读取重点地址 `frozen(address)`，扫描配置保证游标严格前进，各链携带独立周期；outbox 通过原子 claim 确立发送线性化点；官方源在采集边界严格校验页面顺序和 PDF 月份；市场规则继续仅用 100 万深度决定等级，但附带全部退出容量，并单独记录一小时严重脱锚状态。

**Tech Stack:** Python 3.11+、asyncio、aiosqlite、aiohttp、pydantic、BeautifulSoup、pypdf、pytest。

---

### Task 1: EVM 当前冻结状态和单调追赶

**Files:**
- Modify: `usd1_monitor/collectors/evm.py`
- Modify: `usd1_monitor/scheduler.py`
- Modify: `usd1_monitor/config.py`
- Modify: `config.example.yaml`
- Modify: `deploy/config.production.example.yaml`
- Test: `tests/test_config.py`
- Test: `tests/test_evm_scanner.py`
- Test: `tests/test_evm_snapshot.py`
- Test: `tests/test_evm_integration.py`

- [x] 新增失败测试：`scan_batch_blocks <= overlap_blocks` 被配置层和 scanner 拒绝。
- [x] 新增失败测试：冷启动快照中 watched address 的 `frozen(address)=true` 直接建立 RED，false 不产生虚假恢复通知。
- [x] 实现安全区块 `frozen(address)` 读取和快照事实；把默认批次降到适合 45 秒预算的有限范围。
- [x] 运行四个 EVM/config 测试文件。

### Task 2: 每条 EVM 链独立周期

**Files:**
- Modify: `usd1_monitor/config.py`
- Modify: `usd1_monitor/scheduler.py`
- Modify: `usd1_monitor/cli.py`
- Modify: `config.example.yaml`
- Modify: `deploy/config.production.example.yaml`
- Test: `tests/test_config.py`
- Test: `tests/test_cli.py`
- Test: `tests/test_evm_integration.py`

- [x] 新增失败测试：Ethereum/BSC 周期与 market 周期不同且 `_component_checks()` 分别使用链配置。
- [x] 给 `ChainConfig` 和 `EvmChainMonitor` 增加正整数 `interval_seconds`，CLI 显式传递。
- [x] 运行 config/CLI/scheduler 测试。

### Task 3: Outbox 原子 claim 与重组取消

**Files:**
- Modify: `usd1_monitor/storage.py`
- Modify: `usd1_monitor/scheduler.py`
- Test: `tests/test_storage.py`
- Test: `tests/test_wechat.py`
- Test: `tests/test_evm_integration.py`

- [x] 新增失败测试：已 CANCELLED 的告警不能被发送结果复活，FAILED 孤块告警可被重组取消，两个 worker 只能 claim 一次。
- [x] 增加 `IN_FLIGHT` 原子 claim；启动时把崩溃遗留的 IN_FLIGHT 恢复成 PENDING；发送结果只更新 IN_FLIGHT。
- [x] 重组取消 PENDING/FAILED，已 claim 告警以 claim 时点为发送线性化点。
- [x] 运行 storage/通知/EVM 集成测试。

### Task 4: 权限调用真实性与升级告警归因

**Files:**
- Modify: `usd1_monitor/collectors/evm.py`
- Modify: `usd1_monitor/engine/evm_rules.py`
- Modify: `usd1_monitor/scheduler.py`
- Test: `tests/test_evm_snapshot.py`
- Test: `tests/test_evm_integration.py`

- [x] 新增失败测试：普通成功调用 owner/admin 合约不生成权限事实；Safe 只有 `execTransaction` 内部目标为 token/admin 且 receipt 含 ExecutionSuccess 才生成事实。
- [x] 解码 Safe 内层目标/selector，并拒绝 ExecutionFailure、无执行事件和无关内部目标。
- [x] 给快照事实增加独立 `cause_id`；同区块权限 upgrade 与 implementation/code hash 变化合并为一条消息。
- [x] 运行 EVM 单元和集成测试。

### Task 5: 官方 JSON 边界与 BitGo 报告身份

**Files:**
- Modify: `usd1_monitor/http.py`
- Modify: `usd1_monitor/collectors/announcements.py`
- Test: `tests/test_http.py`
- Test: `tests/test_official_sources.py`

- [x] 新增失败测试：JSON 30x 不跟随；BitGo 同月同标记报告按页面顺序选择；链接月份与 PDF 月份不一致时明确失败。
- [x] JSON 请求禁用重定向并把 3xx 作为结构化 HTTP 错误。
- [x] 保留 BitGo DOM 顺序，同月优先显式修订、同优先级取页面首项；严格校验 PDF `report_month`。
- [x] 运行 HTTP/官方源测试。

### Task 6: 市场严重脱锚、退出容量和来源证据

**Files:**
- Modify: `usd1_monitor/config.py`
- Modify: `usd1_monitor/engine/market_rules.py`
- Modify: `usd1_monitor/engine/evm_rules.py`
- Modify: `usd1_monitor/scheduler.py`
- Modify: `usd1_monitor/notifications/wechat.py`
- Modify: `config.example.yaml`
- Modify: `deploy/config.production.example.yaml`
- Test: `tests/test_config.py`
- Test: `tests/test_market_rules.py`
- Test: `tests/test_market_integration.py`
- Test: `tests/test_wechat.py`

- [x] 新增失败测试：`<0.99` 连续一小时建立独立 RED 严重脱锚状态且不降低现有 RED；市场告警包含 100/500/2000 万退出容量。
- [x] 把严重脱锚阈值/持续时间放入 YAML；MarketSnapshot 携带所有 sell size 结果，等级判定仍只使用 100 万。
- [x] 市场和 EVM 生产 evidence 提供可点击官方 API/区块浏览器来源；通知格式展示严重脱锚与退出容量。
- [x] 运行 config/market/通知测试。

### Task 7: 全量验收

**Files:**
- Review: all files changed by Tasks 1-6

- [x] 运行全量离线测试、live 只读测试、compileall、pip check、配置加载和 git diff check。
- [x] 对照本计划逐项确认，不创建提交、不覆盖无关改动。
