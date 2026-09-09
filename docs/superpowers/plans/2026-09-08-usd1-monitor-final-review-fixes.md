# USD1 Monitor Final Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 2026-09-08 最终复审确认的冷启动漏报、EVM 追赶停滞、事务外发、迁移兼容、调度周期和外部数据源可靠性问题。

**Architecture:** 保留单进程 asyncio + SQLite 架构。EVM 每轮只原子提交有限区块且 cursor 单调；待发告警在读取前与数据库写事务同步；调度器为每个组件维护自己的唤醒周期；外部数据解析在采集边界完成严格校验与资源限制。

**Tech Stack:** Python 3.11+、asyncio、aiosqlite、aiohttp、pydantic、BeautifulSoup、pypdf、pytest。

---

### Task 1: EVM 冷启动、单调游标与有限追赶

**Files:**
- Modify: `usd1_monitor/collectors/evm.py`
- Modify: `usd1_monitor/scheduler.py`
- Test: `tests/test_evm_scanner.py`
- Test: `tests/test_evm_integration.py`
- Test: `tests/test_evm_snapshot.py`

- [x] 新增失败测试：首次快照 `paused=True` 直接建立 RED；安全头低于 cursor 时拒绝提交旧快照/倒退 cursor。
- [x] 新增失败测试：cursor 大幅落后时每轮最多处理 `scan_batch_blocks`，成功后逐批单调追赶；45 秒超时后下一轮不会重复无限区间。
- [x] 新增失败测试：非 owner/admin 地址向 EOA owner/admin 发送 calldata 不生成权限事实，Safe/ProxyAdmin 的真实执行仍能识别。
- [x] 最小实现：限制目标区间、校验 safe head 单调、解释首次可逆快照，并收紧权限调用判定。
- [x] 运行三个 EVM 测试文件直至通过。

### Task 2: SQLite outbox 与旧库迁移

**Files:**
- Modify: `usd1_monitor/storage.py`
- Modify: `usd1_monitor/scheduler.py`
- Test: `tests/test_storage.py`
- Test: `tests/test_wechat.py`

- [x] 新增失败测试：未提交事务中的 alert 不可被 delivery worker 读取或发送，回滚后无外发记录。
- [x] 新增失败测试：旧 schema 中 SENT、耗尽失败和可重试记录分别迁移成 `SENT/FAILED/PENDING`。
- [x] 新增失败测试：旧 announcement 的列表页哈希首次升级只建立正文基线，不触发虚假 CHANGED/字段变化事件。
- [x] 最小实现：pending 快照读取与 write lock 同步；`open()` 执行幂等状态回填；公告增加内容版本/基线迁移标识。
- [x] 运行 storage、通知和信息集成测试直至通过。

### Task 3: 独立组件周期

**Files:**
- Modify: `usd1_monitor/scheduler.py`
- Modify: `usd1_monitor/cli.py`
- Modify: `usd1_monitor/config.py`
- Modify: `config.example.yaml`
- Modify: `deploy/config.production.example.yaml`
- Test: `tests/test_cli.py`
- Test: `tests/test_config.py`

- [x] 新增失败测试：market、每条 EVM 链、PoR/supply、各官方源按自己的配置周期唤醒，修改 market 周期不延迟 PoR。
- [x] 最小实现：组件检查表携带 interval；run loop 使用组件 interval，不再共享 market interval。
- [x] 运行 CLI/config/scheduler 相关测试直至通过。

### Task 4: Binance 与 RPC 真实故障语义

**Files:**
- Modify: `usd1_monitor/http.py`
- Modify: `usd1_monitor/rpc.py`
- Modify: `usd1_monitor/collectors/market.py`
- Modify: `usd1_monitor/scheduler.py`
- Test: `tests/test_http.py`
- Test: `tests/test_rpc.py`
- Test: `tests/test_market_collector.py`

- [x] 新增失败测试：Binance `HTTP 400/-1121` 映射为明确的 `symbol_trading=false`，其他 4xx 仍抛错。
- [x] 新增失败测试：RPC 切换或重新启用端点时复验 chain ID；错误链端点即使有可用备用也形成可见健康降级。
- [x] 新增失败测试：RPC `error.message` 中的 URL、项目 ID和密钥不进入异常、日志或数据库。
- [x] 最小实现：HTTP 暴露结构化状态/安全响应摘要；市场只识别 Binance -1121；RPC 维护当前端点与校验结果并输出脱敏端点故障。
- [x] 运行 HTTP/RPC/market 测试直至通过。

### Task 5: 官方页面完整性与资源边界

**Files:**
- Modify: `usd1_monitor/http.py`
- Modify: `usd1_monitor/collectors/announcements.py`
- Modify: `usd1_monitor/collectors/attestations.py`
- Test: `tests/test_http.py`
- Test: `tests/test_official_sources.py`
- Test: `tests/test_attestations.py`

- [x] 新增失败测试：详情链接必须 HTTPS、无 userinfo、使用默认 443；响应超过配置上限时中止。
- [x] 新增失败测试：Binance 翻页直到空页/已知边界；第二页 USD1 公告可采集且不会无限翻页。
- [x] 新增失败测试：BitGo 同月多份报告按页面最新修订选择，不按 URL hash 排序。
- [x] 新增失败测试：PDF 解析通过 `asyncio.to_thread` 执行且页数/字节数受限，不阻塞事件循环。
- [x] 最小实现并运行官方源、HTTP、鉴证测试直至通过。

### Task 6: PoR 恢复、告警证据与部署文档

**Files:**
- Modify: `usd1_monitor/engine/reserve_rules.py`
- Modify: `usd1_monitor/scheduler.py`
- Modify: `usd1_monitor/notifications/wechat.py`
- Modify: `deploy/config.production.example.yaml`
- Modify: `README.md`
- Modify: `docs/superpowers/plans/2026-09-07-usd1-monitor-hardening.md`
- Test: `tests/test_reserve_rules.py`
- Test: `tests/test_reserve_supply_integration.py`
- Test: `tests/test_wechat.py`

- [x] 新增失败测试：PoR 恢复必须来自不同 Oracle bundle 时间戳，重复轮询相同 bundle 不恢复。
- [x] 新增失败测试：EVM、reorg、健康告警正文显示事实类型、地址/selector/金额或错误摘要，不再出现无意义 unknown。
- [x] 最小实现；修正旧计划中未实际落地的状态字段勾选说明；生产模板提供第二 Ethereum RPC。
- [x] 运行相关测试、全量离线测试、live 只读测试、compileall、pip check 和 git diff check。

### Task 7: 最终复审遗留边界

**Files:**
- Modify: `usd1_monitor/storage.py`
- Modify: `usd1_monitor/scheduler.py`
- Modify: `usd1_monitor/collectors/announcements.py`
- Test: `tests/test_storage.py`
- Test: `tests/test_wechat.py`
- Test: `tests/test_reserve_supply_integration.py`
- Test: `tests/test_official_sources.py`
- Test: `tests/test_evm_integration.py`

- [x] 新增失败测试：通知发送窗口和失败冷却在 limiter/进程重建后仍生效。
- [x] 新增失败测试：PoR 与 global supply 同轮并发时只使用两项采集完成后的最新输入计算覆盖率。
- [x] 新增失败测试：GitBook 全局 `Agent Instructions` 尾部变化不改变官方正文哈希。
- [x] 新增失败测试：snapshot-only 重组取消对应链上已失效的 `paused/frozen` 待发告警。
- [x] 最小实现：持久化发送预算；覆盖率改为并发采集后按 PoR→供应量顺序提交；剥离明确 GitBook 尾部；按链和区块取消可逆快照告警。
- [x] 运行定向测试、全量离线测试、compileall、pip check 和 git diff check。

本计划不自动创建提交、合并或清理分支；集成方式由用户在最终验收后选择。
