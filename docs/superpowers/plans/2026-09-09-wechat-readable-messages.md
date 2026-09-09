# USD1 微信通知易读化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将企业微信启动、风险和恢复通知改成不含 Markdown 与内部标识的简明中文纯文本。

**Architecture:** 只修改通知展示层。`wechat.py` 负责把既有 `RiskTransition` 证据映射为中文状态、摘要、关键数据、时间、来源和建议；`scheduler.py` 复用同一启动消息格式化函数。风险判断、证据存储、队列去重和发送机制保持不变。

**Tech Stack:** Python 3.12、dataclass 模型、pytest、企业微信 text webhook

---

### Task 1: 建立纯文本消息骨架

**Files:**
- Modify: `tests/test_wechat.py`
- Modify: `usd1_monitor/notifications/wechat.py`

- [ ] **Step 1: 写纯文本基础帮助函数的失败测试**

在 `tests/test_wechat.py` 增加：

从 `usd1_monitor.notifications.wechat` 的现有 import 中同时导入 `_display_time`、`_level_heading` 和 `_source_urls`。

```python
from usd1_monitor.notifications.wechat import (
    _display_time,
    _level_heading,
    _source_urls,
)


def test_plain_text_helpers_render_chinese_status_time_and_sources() -> None:
    assert _level_heading(RiskLevel.YELLOW, recovered=False) == "🟡 USD1 注意"
    assert _level_heading(RiskLevel.GREEN, recovered=True) == "🟢 USD1 已恢复正常"
    assert _display_time(NOW, "Asia/Shanghai") == "2026-09-07 12:30:00（北京时间）"
    assert _source_urls({"source_urls": ["https://one", "https://two"]}) == [
        "https://one",
        "https://two",
    ]
    assert _source_urls({}) == []
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_wechat.py::test_plain_text_helpers_render_chinese_status_time_and_sources -q`

Expected: FAIL；基础帮助函数尚不存在。

- [ ] **Step 3: 在 `wechat.py` 增加纯文本基础格式化函数**

保留 `WeChatNotifier.send_text()` 的 `msgtype=text` payload。加入以下私有映射和帮助函数：

```python
LEVEL_LABELS = {
    RiskLevel.GREEN: "🟢 USD1 正常",
    RiskLevel.YELLOW: "🟡 USD1 注意",
    RiskLevel.RED: "🔴 USD1 危险",
}


def _level_heading(level: RiskLevel, *, recovered: bool) -> str:
    return "🟢 USD1 已恢复正常" if recovered else LEVEL_LABELS[level]


def _display_time(value: object, timezone_name: str) -> str:
    localized = local_iso(value, timezone_name)
    return f"{datetime.fromisoformat(localized):%Y-%m-%d %H:%M:%S}（北京时间）"


def _source_urls(evidence: dict[str, object]) -> list[str]:
    raw = evidence.get("source_urls")
    if isinstance(raw, (list, tuple)):
        return [str(item) for item in raw if item]
    single = evidence.get("source_url")
    return [str(single)] if single else []


def _advice(level: RiskLevel, health_only: bool) -> str:
    if level is RiskLevel.GREEN:
        return "建议：继续观察一段时间。"
    if health_only:
        return "建议：检查监控服务和数据源是否正常。"
    return "建议：请打开信息来源并人工确认。"
```

本任务只增加这些无副作用帮助函数，不切换 `format_transitions()`，确保每次提交都保持现有测试通过。`_level_heading(level, recovered)` 使用 `LEVEL_LABELS`，恢复时返回 `🟢 USD1 已恢复正常`。

- [ ] **Step 4: 运行核心格式测试**

Run: `.venv\Scripts\python.exe -m pytest tests/test_wechat.py -q`

Expected: PASS；本任务尚未切换公开消息格式，原有测试也继续通过。

- [ ] **Step 5: 提交**

```bash
git add tests/test_wechat.py usd1_monitor/notifications/wechat.py
git commit -m "建立微信纯文本通知格式"
```

### Task 2: 为现有风险类型生成直白中文摘要

**Files:**
- Modify: `tests/test_wechat.py`
- Modify: `usd1_monitor/notifications/wechat.py`
- Test: `tests/test_evm_integration.py`
- Test: `tests/test_market_integration.py`
- Test: `tests/test_reserve_supply_integration.py`
- Test: `tests/test_information_integration.py`

- [ ] **Step 1: 写各类通知的失败测试**

覆盖市场、链上、储备供应、官方公告、健康异常和恢复：

```python
def test_alert_message_uses_plain_chinese_summary() -> None:
    content = format_transitions(
        [transition("market.price", RiskLevel.GREEN, RiskLevel.YELLOW)]
    )

    assert content.startswith("🟡 USD1 注意")
    assert "发生了什么：USD1 价格低于预警线" in content
    assert "当前价格：0.996" in content
    assert "预警价格：0.997" in content
    assert "发现时间：2026-09-07 12:30:00（北京时间）" in content
    assert "信息来源：\nhttps://api.binance.com/api/v3/depth" in content
    assert "建议：请打开信息来源并人工确认。" in content
    for hidden in ("market.price", "rule_id", "当前值=", "阈值=", "GREEN", "YELLOW"):
        assert hidden not in content
    assert not any(token in content for token in ("##", "**", "- ", "[", "]("))


@pytest.mark.parametrize(
    ("rule_id", "evidence", "expected"),
    (
        ("market.liquidity", {"current": 0.994}, "USD1 市场流动性不足"),
        ("por.age", {"current": 8000}, "USD1 储备数据长时间没有更新"),
        ("supply.estimated_coverage", {"current": 98.4}, "USD1 估算储备覆盖率异常"),
        (
            "event.information.binance.notice.hash",
            {"threshold": "official risk phrase: USD1 withdrawals are suspended"},
            "官方公告提到 USD1 withdrawals are suspended",
        ),
        ("health.evm_bsc", {"last_error": "timeout"}, "BNB Chain 数据暂时无法获取"),
    ),
)
def test_existing_rule_types_have_human_summary(rule_id, evidence, expected) -> None:
    item = transition(rule_id, RiskLevel.GREEN, RiskLevel.YELLOW)
    item.evidence.update(evidence)
    assert expected in format_transitions([item])
```

为链上事件增加独立测试，断言显示 `Ethereum`、中文事件名、必要地址或金额，但不显示 `fact_type=`、`event_key=`、`selector=` 或原因哈希。为未知规则增加测试，断言输出“监控发现异常，请打开信息来源并人工确认”，且不回显未知 `rule_id`。

- [ ] **Step 2: 运行新增测试并确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_wechat.py -q`

Expected: FAIL；尚无风险类型中文映射。

- [ ] **Step 3: 实现固定中文摘要映射**

在 `wechat.py` 增加固定映射，不调用 AI：

```python
CHAIN_LABELS = {"ethereum": "Ethereum", "bsc": "BNB Chain"}
COLLECTOR_LABELS = {
    "binance_market": "Binance 市场",
    "evm_ethereum": "Ethereum",
    "evm_bsc": "BNB Chain",
    "por": "储备证明",
    "native_supply": "链上供应量",
    "defillama_supply": "全链供应量估算",
    "official_binance": "Binance 官方公告",
    "official_bitgo": "BitGo 官方信息",
    "official_wlfi": "WLFI 官方信息",
    "official_occ": "OCC 官方信息",
    "notification_wechat": "企业微信通知",
}
FACT_LABELS = {
    "PAUSED": "USD1 合约已暂停",
    "FREEZE": "地址被 USD1 合约冻结",
    "IMPLEMENTATION_CHANGED": "USD1 实现合约发生变化",
    "ADMIN_CHANGED": "USD1 管理员地址发生变化",
    "CODE_HASH_CHANGED": "USD1 合约代码发生变化",
    "OWNER_CHANGED": "USD1 所有者地址发生变化",
    "PRIVILEGED_UNKNOWN_CALL": "检测到未知的高权限合约调用",
    "MINT": "检测到大额 USD1 铸造",
    "BURN": "检测到大额 USD1 销毁",
}
```

实现 `_human_summary(transition) -> tuple[str, list[str]]`：

- `market.price` 与 `market.price.severe` 输出价格偏离摘要、当前价格和对应阈值。
- `market.liquidity`、`market.trading` 输出流动性或交易状态摘要。
- `por.age`、`por.reserve_change`、`supply.estimated_coverage`、`supply.native_drop_24h` 输出储备供应摘要。
- `event.information.*` 从 `threshold` 去掉 `official risk phrase: ` 前缀，输出公告风险语句；`information.attestation_*` 输出鉴证报告中文摘要。
- `evm.*` 与 `event.evm.*` 根据 `fact_type`、`chain`、`account`、`from_address`、`to_address`、`amount` 生成中文行。
- `health.*` 使用 `COLLECTOR_LABELS`，不向微信输出 `last_error` 原文；错误细节保留在日志和数据库。
- 未知规则使用通用摘要，不回显规则编号。

展示等级取“当前整体等级”和“本批事件等级”中的较高值，避免短时事件在整体仍为绿色时显示成正常。只有整体为绿色、且本批转换全部恢复为绿色时，标题才使用“已恢复正常”。

金额、价格、比率和字典使用短格式；禁止直接输出大型 `exit_capacity` 字典。多条同原因转换合并为一个事件段；不同原因使用 `事件 1`、`事件 2`，不得输出 `cause_id`。

- [ ] **Step 4: 更新受影响的集成测试断言**

将依赖旧格式的断言改为外部行为断言，例如：

```python
assert "🔴 USD1 危险" in message
assert "USD1 合约已暂停" in message
assert "信息来源：" in message
assert "evm.ethereum.paused" not in message
assert "fact_type=" not in message
```

不要修改风险等级、触发次数或 pending alert 数量断言。

- [ ] **Step 5: 运行通知及直接集成测试**

Run: `.venv\Scripts\python.exe -m pytest tests/test_wechat.py tests/test_evm_integration.py tests/test_market_integration.py tests/test_reserve_supply_integration.py tests/test_information_integration.py -q`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add tests/test_wechat.py tests/test_evm_integration.py tests/test_market_integration.py tests/test_reserve_supply_integration.py tests/test_information_integration.py usd1_monitor/notifications/wechat.py
git commit -m "完善微信风险中文摘要"
```

### Task 3: 简化启动通知

**Files:**
- Modify: `tests/test_market_integration.py`
- Modify: `tests/test_evm_integration.py`
- Modify: `usd1_monitor/notifications/wechat.py`
- Modify: `usd1_monitor/scheduler.py`

- [ ] **Step 1: 写两条启动消息失败测试**

分别覆盖只启用市场监控和启用完整调度器：

```python
assert message.startswith("🟢 USD1 监控已启动")
assert "正在监控：\n价格、流动性" in message
assert "暂未覆盖：" in message
assert "社交媒体情绪" in message
for hidden in ("enabled_collectors", "NOT_MONITORED", "binance_market", "evm_bsc"):
    assert hidden not in message
```

同时断言 notifier 收到的 payload 仍为 `{"msgtype": "text", ...}`。

- [ ] **Step 2: 运行测试并确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_market_integration.py tests/test_evm_integration.py -q`

Expected: FAIL；当前启动消息仍使用内部采集器名称。

- [ ] **Step 3: 添加并复用启动消息格式化函数**

在 `wechat.py` 增加：

```python
NOT_MONITORED_LABELS = {
    "private_exchange_account": "私人交易所账户",
    "active_conversion_probe": "主动兑换测试",
    "tron_solana_aptos_tempo_bridges": "部分跨链桥",
    "binance_wallet_concentration": "Binance 钱包集中度",
    "social_media_sentiment": "社交媒体情绪",
    "defi_liquidations": "DeFi 清算",
    "web_dashboard": "网页仪表盘",
    "full_multichain_supply_reconciliation": "完整多链供应量对账",
}


def format_startup_message(
    monitored: Iterable[str], not_monitored: Iterable[str]
) -> str:
    active = "、".join(dict.fromkeys(monitored))
    missing = "、".join(
        NOT_MONITORED_LABELS.get(item, "其他未覆盖能力")
        for item in not_monitored
    )
    return (
        "🟢 USD1 监控已启动\n\n"
        f"正在监控：\n{active}\n\n"
        f"暂未覆盖：\n{missing}"
    )
```

两个 `send_startup_once()` 都调用该函数。市场监控传入 `("价格", "流动性", "交易状态")`；完整调度器根据实际启用组件追加“Ethereum 链上合约”“BNB Chain 链上合约”“储备与供应量”“官方公告”。不改发送频率、限速和失败健康记录。

- [ ] **Step 4: 运行启动和通知测试**

Run: `.venv\Scripts\python.exe -m pytest tests/test_market_integration.py tests/test_evm_integration.py tests/test_wechat.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add tests/test_market_integration.py tests/test_evm_integration.py usd1_monitor/notifications/wechat.py usd1_monitor/scheduler.py
git commit -m "简化微信启动通知"
```

### Task 4: 文档与完整验收

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/plans/2026-09-09-wechat-readable-messages.md`

- [ ] **Step 1: 更新 README 通知语义**

在现有通知语义段明确：企业微信只发送纯文本；状态显示为正常、注意、危险；消息不含内部规则编号和原因哈希；详细诊断仍在日志与数据库中。

- [ ] **Step 2: 运行完整验证**

Run: `.venv\Scripts\python.exe -m pytest -q`

Expected: 全部离线测试通过，live 测试仅按既有标记跳过。

Run: `.venv\Scripts\python.exe -m compileall -q usd1_monitor tests`

Expected: exit code 0，无输出。

Run: `.venv\Scripts\python.exe -m pip check`

Expected: `No broken requirements found.`

Run: `git diff --check`

Expected: exit code 0，无空白错误。

- [ ] **Step 3: 做范围自审**

检查 `git diff --stat`、`git diff` 和 `git status --short`。只允许出现设计中列出的通知、调度器、测试、README 和计划文件；确认未修改风险判断、阈值、数据库结构或 webhook 配置。

- [ ] **Step 4: 提交最终文档状态**

```bash
git add README.md docs/superpowers/plans/2026-09-09-wechat-readable-messages.md
git commit -m "记录微信通知易读化验收"
```
