# USD1 Monitor Readiness Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复最终就绪审查确认的五个漏报、误判与重组一致性问题，并用回归测试锁定行为。

**Architecture:** 保留现有 collector → observation/event → state engine → SQLite 流程。修复集中在数据进入边界校验、状态事实生成、时间连续性筛选、重组告警 cause 生命周期和 Binance 详情候选选择，不改公共配置格式或部署结构。

**Tech Stack:** Python 3.11+、asyncio、aiohttp、SQLite/aiosqlite、pytest。

---

### Task 1: paused 支持状态迁移

**Files:**
- Modify: `tests/test_evm_integration.py`
- Modify: `usd1_monitor/scheduler.py`

- [x] 增加 unsupported → supported true/false 的失败集成测试，断言分别建立 RED/GREEN 状态。
- [x] 运行目标测试并确认因 `_snapshot_facts()` 未产生事实而失败。
- [x] 将首次支持视为首次有效状态观测，生成 PAUSED/UNPAUSED 事实。
- [x] 运行 EVM 集成测试并确认通过。

### Task 2: 覆盖率小时点连续性

**Files:**
- Modify: `tests/test_reserve_supply_integration.py`
- Modify: `usd1_monitor/scheduler.py`

- [x] 增加 24 小时断档后第二个低覆盖率点不触发 YELLOW 的失败测试。
- [x] 运行目标测试并确认最后两个数值被错误当成连续点。
- [x] 在调度层按 `SupplyConfig.interval_seconds` 和有限调度容差筛选连续点，再调用现有纯规则。
- [x] 运行覆盖率集成与规则测试并确认正常一小时点、同周期替换和断档行为均通过。

### Task 3: 区块哈希完整性

**Files:**
- Modify: `tests/test_evm_snapshot.py`
- Modify: `usd1_monitor/collectors/evm.py`

- [x] 增加 missing、short、non-hex block hash 均抛出 `EvmScanError` 的失败测试，并为现有成功样本补合法哈希。
- [x] 运行目标测试并确认非法哈希当前被静默接受。
- [x] 严格校验每个全块响应的 32 字节十六进制哈希。
- [x] 运行 EVM collector/scanner/integration 测试并确认通过。

### Task 4: 重组修正告警生命周期

**Files:**
- Modify: `tests/test_storage.py`
- Modify: `tests/test_evm_integration.py`
- Modify: `usd1_monitor/scheduler.py`
- Modify: `usd1_monitor/storage.py`

- [x] 增加按链取消 `reorg-snapshot:<chain>:<block>` 告警以及连续两次重组的失败测试。
- [x] 运行目标测试并确认旧修正告警仍为待发送。
- [x] 给新重组快照 cause 加链名，并扩展取消解析同时覆盖 `snapshot:`、`reorg-snapshot:` 与必要旧格式。
- [x] 运行 storage 和 EVM 集成测试并确认其他链同高度告警不受影响。

### Task 5: Binance 正文独有 USD1

**Files:**
- Modify: `tests/test_official_sources.py`
- Modify: `usd1_monitor/collectors/announcements.py`
- Modify: `usd1_monitor/storage.py`
- Modify: `usd1_monitor/cli.py`

- [x] 增加“标题无 USD1、详情正文有 USD1”失败测试，并断言无关正文仍被排除。
- [x] 运行目标测试并确认详情请求在标题过滤前无法发生。
- [x] 每轮优先检查标题命中条目和数据库已跟踪 Binance ID，并将最多 8 条中性标题正文检查结果持久化为续扫进度；首次启动和积压场景不会永久丢失候选，扫描记录满 24 小时后轮转复查正文更新。
- [x] 运行公告、信息集成和 live 公开源测试并确认通过。

### Task 6: 全量验证

**Files:**
- Verify only.

- [x] 运行全部离线 pytest。
- [x] 运行四项只读 live 冒烟测试。
- [x] 运行 `compileall`、`pip check` 和 scoped `git diff --check`。
- [x] 对照五项审查 finding 复核代码与测试证据。

### Task 7: Binance 正文扫描队列与部分失败进度

**Files:**
- Modify: `tests/test_official_sources.py`
- Modify: `tests/test_information_integration.py`
- Modify: `usd1_monitor/collectors/announcements.py`
- Modify: `usd1_monitor/scheduler.py`
- Modify: `usd1_monitor/storage.py`
- Modify: `usd1_monitor/cli.py`

- [x] 增加“过期复扫记录不得挤占从未扫描记录”的失败测试，并确认旧实现会漏掉更旧的首次候选。
- [x] 将正文候选拆分为首次扫描与过期复扫两个队列，固定优先消化首次扫描积压。
- [x] 增加单条详情失败仍保存其他扫描进度、下一轮继续推进的失败测试。
- [x] 单条详情失败时继续处理本批其他条目，即时保存中性扫描记录，并通过部分失败异常返回已发现的风险项与失败详情。
- [x] 调度层保存部分成功的风险项、记录采集失败健康状态，并按正常间隔安排下一轮检查。
- [x] 首扫候选按 stable ID 去重并使用 FIFO；详情失败记录进入 24 小时冷却，避免失败项或持续新公告永久占满扫描额度。
- [x] Binance 详情采用最多 8 个并发请求和每项 10 秒总预算，慢请求不会阻塞同批其他风险结果。
- [x] 增加 8 条持续失败、慢详情和扫描记录写入失败传播测试；普通失败不推进来源调度时间。
- [x] 标题命中与已跟踪条目同样遵守详情失败冷却；冷却中的失败以 deferred partial 状态保留，不会误报采集恢复。
- [x] 部分失败仅在健康状态成功写入后推进来源调度时间；健康写入失败时允许下一轮立即重试。
- [x] 严格要求 Binance 公告提供有效 `releaseDate`，保证首次扫描 FIFO 顺序可判定。
- [x] 运行相关测试、全部离线测试、公开源 live 冒烟、`compileall`、`pip check` 与 scoped `git diff --check`。

### Task 8: Binance 公平队列与失败状态边界

**Files:**
- Modify: `tests/test_official_sources.py`
- Modify: `tests/test_information_integration.py`
- Modify: `tests/test_storage.py`
- Modify: `tests/test_cli.py`
- Modify: `usd1_monitor/collectors/announcements.py`

- [x] 标题/known 与正文候选交错进入详情队列，保证正文 FIFO 在首批并发中获得执行槽位。
- [x] deferred 状态从全部近期失败 ID 初始化，失败公告暂时跌出分页时不再误报采集恢复。
- [x] `releaseDate` 仅接受合理时间范围内的整数毫秒，拒绝负值、零值、秒级、浮点和明显未来时间。
- [x] 直接覆盖失败 ID 的来源、TTL、metadata 过滤，并验证默认 CLI builder 的四项持久化接线。
- [x] 运行影响面测试、全部离线测试、公开源 live 冒烟、`compileall`、`pip check` 与 scoped `git diff --check`。
