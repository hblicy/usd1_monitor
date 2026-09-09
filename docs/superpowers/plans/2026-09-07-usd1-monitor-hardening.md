# USD1 Monitor Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复审查发现的永久漏报、错误恢复、官方内容漏采、调度耦合和运维盲区，使实现满足已确认的 USD1 风险监控设计。

**Architecture:** 保持 Python/asyncio/SQLite 单进程架构，但把“采集事实、解释事实、提交状态、推进游标”收敛为可重放事务；各 collector 使用独立周期和健康状态。业务总体风险、监控健康和通知交付状态分别聚合。

**Tech Stack:** Python 3.11、asyncio、aiosqlite、aiohttp、pydantic、BeautifulSoup、pypdf、pytest。

---

### Task 1: 链上事实的可重放事务

**Files:**
- Modify: `usd1_monitor/storage.py`
- Modify: `usd1_monitor/collectors/evm.py`
- Modify: `usd1_monitor/scheduler.py`
- Test: `tests/test_evm_scanner.py`
- Test: `tests/test_evm_integration.py`
- Test: `tests/test_storage.py`

- [x] 新增回归测试：scanner 已取到日志、后续 snapshot 或 StateEngine 失败时 cursor 不推进；重启后相同日志仍进入规则引擎。
- [x] 新增回归测试：事件、快照、状态、pending alert 与 cursor 在同一事务中提交，任一步失败全部回滚。
- [x] 在 `Storage` 增加显式事务上下文和不自动 commit 的批量写入方法；现有单条写入默认行为保持兼容。
- [x] 调整 `EvmScanner` 只返回候选事件和目标 safe head，不在 scanner 内推进 cursor。
- [x] 由 `EvmChainMonitor` 完成 token logs、权限交易、快照和规则解释后原子提交，并最后更新 cursor。
- [x] 保存 `block_hash`；重叠窗口发现 canonical hash 变化时撤销共同祖先后的未确认事实并重新解释。
- [x] 运行：`.venv\\Scripts\\python.exe -m pytest tests/test_evm_scanner.py tests/test_evm_integration.py tests/test_storage.py -q`。

### Task 2: 市场状态及跨重启持续时间

**Files:**
- Modify: `usd1_monitor/collectors/market.py`
- Modify: `usd1_monitor/engine/market_rules.py`
- Modify: `usd1_monitor/storage.py`
- Modify: `usd1_monitor/scheduler.py`
- Test: `tests/test_market_collector.py`
- Test: `tests/test_market_rules.py`
- Test: `tests/test_market_integration.py`

- [x] 新增回归测试：symbol 不存在时产生 `market.symbol_trading=false`，且不请求/不依赖 depth。
- [x] 新增重启测试：低价触发起点、连续计数和恢复起点从 SQLite 重建，不因重启延后或提前。
- [x] 先解析 `exchangeInfo`；只有 TRADING symbol 才读取 depth，区分“有效确认不存在”和“查询失败”。
- [x] 使用持久化 observation 历史与现有 risk state 重建低价起点、连续计数和恢复窗口；无需新增状态表字段。
- [x] 运行：`.venv\\Scripts\\python.exe -m pytest tests/test_market_collector.py tests/test_market_rules.py tests/test_market_integration.py -q`。

### Task 3: PoR、供应量与事件状态生命周期

**Files:**
- Modify: `usd1_monitor/engine/reserve_rules.py`
- Modify: `usd1_monitor/engine/supply_rules.py`
- Modify: `usd1_monitor/engine/evm_rules.py`
- Modify: `usd1_monitor/scheduler.py`
- Test: `tests/test_reserve_rules.py`
- Test: `tests/test_reserve_supply_integration.py`
- Test: `tests/test_supply_rules.py`
- Test: `tests/test_evm_rules.py`

- [x] 新增状态机测试：PoR 黄/红必须连续两次新鲜读取才恢复。
- [x] 新增测试：供应小时点按持久化小时桶去重，并验证相邻有效点时间间隔。
- [x] 新增测试：覆盖率需连续两个合格小时点恢复；短时间重启不能伪造连续小时点。
- [x] 新增测试：大额 mint/burn 黄灯在配置窗口结束后恢复 GREEN，历史事件仍保留。
- [x] 在 scheduler 中使用 `valid_recovery` 和 prior state；供应规则使用带时间的持久化读数；为事件型状态设置 `expires_at` 并定期重算。
- [x] 运行相关四个测试文件。

### Task 4: 官方正文与鉴证 PDF

**Files:**
- Modify: `usd1_monitor/collectors/announcements.py`
- Modify: `usd1_monitor/collectors/attestations.py`
- Modify: `usd1_monitor/models.py`
- Modify: `usd1_monitor/storage.py`
- Modify: `usd1_monitor/scheduler.py`
- Test: `tests/test_announcements.py`
- Test: `tests/test_attestations.py`
- Test: `tests/test_information_integration.py`
- Test: `tests/test_official_sources.py`

- [x] 新增集成测试：同 URL/标题下正文变化产生 CHANGED；相同正文不重复通知。
- [x] 新增真实布局夹具测试：PDF bytes hash、月份、tokens outstanding、redemption assets、资产类别、托管人和审计机构均可提取；缺关键字段产生黄色解析告警。
- [x] collector 在官方域名白名单内下载详情页/PDF，正文 hash 使用规范化正文，PDF hash 使用原始 bytes。
- [x] 增加鉴证结构化表并比较相邻报告；托管人、审计机构、资产类别变化产生 YELLOW。
- [x] 为公告事件增加可配置活动窗口/确认机制，避免历史信息永久抬高总体状态。
- [x] 运行相关四个测试文件。

### Task 5: 聚合、健康与通知可靠性

**Files:**
- Create: `usd1_monitor/engine/aggregate.py`
- Modify: `usd1_monitor/engine/health.py`
- Modify: `usd1_monitor/engine/state.py`
- Modify: `usd1_monitor/notifications/wechat.py`
- Modify: `usd1_monitor/storage.py`
- Modify: `usd1_monitor/cli.py`
- Test: `tests/test_health.py`
- Test: `tests/test_wechat.py`
- Test: `tests/test_status_output.py`

- [x] 新增测试：一条规则恢复但另一条仍 RED 时，通知总体仍为 RED。
- [x] 新增测试：业务风险与 monitor health 分开展示；冷启动关键 collector 连续 15 分钟失败升级 RED。
- [x] 新增测试：通知两次失败进入 FAILED/dead-letter，status 可见且 notification health 非 GREEN。
- [x] 聚合器从完整状态快照计算 `business_overall` 与 `monitor_health`；通知 formatter 接收已聚合等级。
- [x] collector health 保存首次失败时间；alert delivery 保存 PENDING/SENT/FAILED 和最后错误，不再静默丢弃。
- [x] 运行相关三个测试文件。

### Task 6: 独立调度、RPC 验证与权限调用

**Files:**
- Modify: `usd1_monitor/rpc.py`
- Modify: `usd1_monitor/http.py`
- Modify: `usd1_monitor/collectors/evm.py`
- Modify: `usd1_monitor/scheduler.py`
- Modify: `usd1_monitor/cli.py`
- Test: `tests/test_rpc.py`
- Test: `tests/test_http.py`
- Test: `tests/test_evm_integration.py`

- [x] 新增测试：每个 RPC endpoint 的 `eth_chainId` 必须匹配配置；错误链端点被拒绝并形成健康错误。
- [x] 新增测试：URL userinfo、query、fragment、路径型 API key 和底层异常文本不会泄漏。
- [x] 新增测试：一个 supply 来源失败不阻止其他来源落库，各自维护 health。
- [x] 新增测试：ProxyAdmin owner、upgrade selector、receipt status；失败交易不形成事实。
- [x] `Usd1Monitor.run` 为市场、各链、PoR、供应和官方源建立独立周期任务，设置超时并隔离异常。
- [x] RPC 切换时校验 chain ID；权限调用按有限 batch 扫描，保存明确的未覆盖能力状态。
- [x] 运行相关三个测试文件。

### Task 7: 运维闭环

**Files:**
- Modify: `usd1_monitor/config.py`
- Modify: `usd1_monitor/cli.py`
- Modify: `usd1_monitor/scheduler.py`
- Modify: `usd1_monitor/logging_config.py`
- Modify: `README.md`
- Modify: `config.example.yaml`
- Modify: `deploy/config.production.example.yaml`
- Test: `tests/test_config.py`
- Test: `tests/test_cli.py`
- Test: `tests/test_status_output.py`

- [x] 新增测试：`.env` 被加载但不覆盖显式环境变量；无效 ZoneInfo 配置失败。
- [x] 新增测试：CLI/微信按配置时区显示；固定状态清单输出 FACT/ESTIMATED/UNKNOWN/NOT_MONITORED。
- [x] 新增测试：180 天 observation prune 被低频维护任务调用；SIGTERM 触发有序停止并关闭资源。
- [x] 增加 retention 配置、时区转换、dotenv 初始化、stop event 和维护任务；同步示例与 README。
- [x] 运行相关三个测试文件。

### Task 8: 全量验收

**Files:**
- Review: `方案.md`
- Review: `docs/superpowers/specs/2026-09-07-usd1-risk-monitor-design.md`
- Review: all changed source/tests/config/deploy files

- [x] 运行 `.venv\\Scripts\\python.exe -m pytest -q`，要求零失败；live 测试保持显式 opt-in。
- [x] 运行 `.venv\\Scripts\\python.exe -m py_compile usd1_monitor\\*.py usd1_monitor\\collectors\\*.py usd1_monitor\\engine\\*.py usd1_monitor\\notifications\\*.py`。
- [x] 执行 `python -m usd1_monitor status` 和使用 fake sources 的 `check`，核对业务总体、健康、交付和质量标签。
- [x] 检查 `git diff --check`、目标目录 git diff 和敏感信息模式。
- [x] 对照设计第 14 节逐项记录通过或未通过；未验证的 live 能力明确列出，不宣称完成。

### Task 9: 最终复审追加修复

- [x] 不可逆 EVM 事实不再按临时事件窗口过期或清理；冷启动状态明确显示 UNKNOWN。
- [x] PoR 储备变化恢复要求独立采集且数值稳定，避免连续下降或重复轮询误恢复。
- [x] 供应量使用安全区块时间戳并校验新鲜度；单来源失败隔离且不会高频重试。
- [x] 市场恢复要求全部预期交易对完整健康；同轮市场告警共享因果标识。
- [x] 未投递通知会持续抬高监控健康等级；健康类通知标题不再误标为业务风险。
- [x] HTTP 异常链完成脱敏；BitGo 解析、鉴证截止日和日志时区行为补齐回归测试。
- [x] 独立只读复审确认没有本次修改直接引入的 Critical 或 Important 遗留问题。
