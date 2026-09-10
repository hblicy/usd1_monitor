# USD1 只读网页仪表盘 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 增加一个仅监听本机、只读 SQLite、每 30 秒自动刷新的响应式 USD1 风险总览网页。

**Architecture:** 新增独立 `dashboard` CLI 路径，不进入现有 `Storage.open()` 或监控器构建流程。`DashboardRepository` 以 SQLite `mode=ro` 和 `query_only` 生成一致快照，`aiohttp` 服务只提供静态资源和单个汇总 API，原生 HTML/CSS/JavaScript 展示风险、健康、指标与最近事件。

**Tech Stack:** Python 3.11+、aiohttp、aiosqlite、SQLite WAL、原生 HTML/CSS/JavaScript、pytest/pytest-asyncio。

---

## 文件职责

- Create: `usd1_monitor/dashboard_data.py` — 只读数据库连接、快照查询、指标计算和输出清理。
- Create: `usd1_monitor/dashboard_server.py` — aiohttp 路由、安全响应头和独立服务生命周期。
- Create: `usd1_monitor/dashboard_static/index.html` — 风险优先页面结构。
- Create: `usd1_monitor/dashboard_static/dashboard.css` — 响应式布局和风险状态样式。
- Create: `usd1_monitor/dashboard_static/dashboard.js` — 30 秒轮询和安全 DOM 渲染。
- Modify: `usd1_monitor/config.py` — `DashboardConfig(port)`。
- Modify: `usd1_monitor/cli.py` — `dashboard` 命令在打开普通 `Storage` 前分流。
- Modify: `usd1_monitor/scheduler.py` — 从 `NOT_MONITORED` 删除 `web_dashboard`。
- Create: `tests/test_dashboard_data.py` — 只读仓库、状态、指标和最近事件测试。
- Create: `tests/test_dashboard_server.py` — HTTP、错误和安全头测试。
- Create: `tests/test_dashboard_assets.py` — 页面结构、轮询周期和无外部依赖测试。
- Modify: `tests/test_config.py` — dashboard 配置边界。
- Modify: `tests/test_cli.py` — dashboard 分流和不创建数据库。
- Modify: `tests/test_status_output.py` — 覆盖范围更新。
- Modify: `config.example.yaml` — dashboard 默认端口。
- Modify: `deploy/config.production.example.yaml` — 生产端口示例。
- Create: `deploy/usd1-dashboard.service` — 独立只读网页服务。
- Modify: `README.md` — 命令、SSH 隧道和 systemd 部署说明。

## Task 1: Dashboard 配置与 CLI 只读分流

**Files:**
- Modify: `tests/test_config.py`
- Modify: `tests/test_cli.py`
- Modify: `usd1_monitor/config.py`
- Modify: `usd1_monitor/cli.py`

- [ ] **Step 1: 写 dashboard 配置失败测试**

在 `tests/test_config.py` 增加：

```python
def test_dashboard_config_defaults_to_unprivileged_port(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("database_path: monitor.db\n", encoding="utf-8")

    config = load_config(path, environ={})

    assert config.dashboard.port == 8080


@pytest.mark.parametrize("port", [1023, 65536])
def test_dashboard_config_rejects_port_outside_safe_range(
    tmp_path: Path, port: int
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"database_path: monitor.db\ndashboard:\n  port: {port}\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError):
        load_config(path, environ={})
```

- [ ] **Step 2: 写 CLI 分流失败测试**

在 `tests/test_cli.py` 增加 `DashboardRunner` 假实现，并证明 dashboard 命令不会创建数据库：

```python
@pytest.mark.asyncio
async def test_dashboard_command_does_not_open_writable_storage(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    database_path = tmp_path / "missing.db"
    write_config(config_path, database_path)
    captured: list[AppConfig] = []

    async def dashboard_runner(config: AppConfig) -> None:
        captured.append(config)

    result = await async_main(
        ["--config", str(config_path), "dashboard"],
        dashboard_runner=dashboard_runner,
    )

    assert result == 0
    assert captured[0].database_path == database_path
    assert database_path.exists() is False
```

- [ ] **Step 3: 运行测试并确认失败**

Run: `python -m pytest tests/test_config.py tests/test_cli.py -q`

Expected: 因 `DashboardConfig`、`dashboard` 命令和 `dashboard_runner` 尚不存在而失败。

- [ ] **Step 4: 实现最小配置和 CLI 分流**

在 `usd1_monitor/config.py` 增加：

```python
class DashboardConfig(StrictModel):
    port: int = Field(default=8080, ge=1024, le=65535)
```

并在 `AppConfig` 增加：

```python
dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
```

在 `usd1_monitor/cli.py`：

1. 将 command choices 改为 `("check", "run", "status", "dashboard")`。
2. 定义 `DashboardRunner = Callable[[AppConfig], Awaitable[None]]`。
3. 给 `async_main` 增加可选 `dashboard_runner` 参数。
4. 配置加载成功后、构造 `Storage` 前分流：

```python
if args.command == "dashboard":
    if dashboard_runner is None:
        from usd1_monitor.dashboard_server import run_dashboard

        dashboard_runner = run_dashboard
    await dashboard_runner(config)
    return 0
```

局部导入避免普通 `check/run/status` 启动路径加载网页资源。

- [ ] **Step 5: 运行配置和 CLI 测试**

Run: `python -m pytest tests/test_config.py tests/test_cli.py -q`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add usd1_monitor/config.py usd1_monitor/cli.py tests/test_config.py tests/test_cli.py
git commit -m "增加网页仪表盘启动配置"
```

## Task 2: 只读仓库与风险/健康状态

**Files:**
- Create: `usd1_monitor/dashboard_data.py`
- Create: `tests/test_dashboard_data.py`

- [ ] **Step 1: 写数据库不存在和只读保护失败测试**

在 `tests/test_dashboard_data.py` 增加：

```python
@pytest.mark.asyncio
async def test_repository_rejects_missing_database_without_creating_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "missing.db"
    repository = DashboardRepository(path)

    with pytest.raises(DashboardDataError, match="does not exist"):
        await repository.open()

    assert path.exists() is False


@pytest.mark.asyncio
async def test_repository_connection_is_query_only(storage) -> None:
    repository = DashboardRepository(storage.path)
    await repository.open()
    try:
        with pytest.raises(aiosqlite.OperationalError, match="readonly"):
            await repository.connection.execute(
                "INSERT INTO risk_states VALUES ('x', 0, 'a', 'b')"
            )
    finally:
        await repository.close()
```

- [ ] **Step 2: 写状态分离和 UNKNOWN 失败测试**

使用现有 `Storage` fixture 写入一个业务 RED 和一个健康 YELLOW；断言：

```python
snapshot = await repository.snapshot()
assert snapshot["business"]["level"] == "RED"
assert snapshot["health"]["level"] == "YELLOW"
assert [item["rule_id"] for item in snapshot["business"]["items"]] == [
    "market.price"
]
assert [item["rule_id"] for item in snapshot["health"]["items"]] == [
    "health.evm_bsc"
]
```

另建只有 schema、没有状态的数据文件，断言两个总体等级都为 `UNKNOWN`，不是 `GREEN`。

- [ ] **Step 3: 写可读告警正文关联失败测试**

在相同时间写 `risk_states.changed_at` 和 `alert_deliveries.created_at`，断言当前风险项使用通知正文。再写没有对应通知的 `por.age`，断言摘要为 `USD1 储备数据长时间没有更新`，而不是裸 `por.age`。

- [ ] **Step 4: 运行测试并确认失败**

Run: `python -m pytest tests/test_dashboard_data.py -q`

Expected: 因 `DashboardRepository` 尚不存在而失败。

- [ ] **Step 5: 实现最小只读仓库**

在 `usd1_monitor/dashboard_data.py` 定义：

```python
REQUIRED_TABLES = {
    "risk_states",
    "observations",
    "collector_health",
    "alert_deliveries",
    "chain_events",
    "announcements",
}


class DashboardDataError(RuntimeError):
    pass


class DashboardRepository:
    def __init__(
        self,
        path: Path,
        *,
        timezone_name: str = "Asia/Shanghai",
        event_active_seconds: int = 3600,
    ) -> None:
        self.path = path
        self.timezone_name = timezone_name
        self.event_active_seconds = event_active_seconds
        self._connection: aiosqlite.Connection | None = None
```

`open()` 必须先用 `path.is_file()` 拒绝缺失文件，再通过 `path.resolve().as_uri() + "?mode=ro"` 和 `uri=True` 打开，设置 `row_factory`、`PRAGMA query_only=ON`、`PRAGMA busy_timeout=1000`，并验证 `sqlite_master` 包含 `REQUIRED_TABLES`。任何失败关闭连接并抛 `DashboardDataError`。

`snapshot()` 使用 `BEGIN`/`COMMIT` 包围全部 SELECT；异常时 `ROLLBACK` 并保留原异常上下文。状态行转换为 `RiskState`，有状态时调用现有 `business_overall()`/`health_overall()`，无状态时明确输出 `UNKNOWN`。

关联可读正文使用：

```sql
SELECT content
FROM alert_deliveries
WHERE created_at = ? AND status != 'CANCELLED'
ORDER BY id
```

没有关联正文时通过固定 `RULE_LABELS` 映射 `market.*`、`por.*`、`supply.*`、`evm.*` 和 `health.*` 常见规则；未知项显示“监控规则发生变化”，并单独保留 `rule_id` 供排错。

- [ ] **Step 6: 运行状态测试**

Run: `python -m pytest tests/test_dashboard_data.py -q`

Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add usd1_monitor/dashboard_data.py tests/test_dashboard_data.py
git commit -m "读取网页仪表盘风险状态"
```

## Task 3: 核心指标与 24 小时供应量变化

**Files:**
- Modify: `usd1_monitor/dashboard_data.py`
- Modify: `tests/test_dashboard_data.py`

- [ ] **Step 1: 写核心指标失败测试**

通过现有 `Storage.insert_observation()` 插入新旧两批数据，覆盖：

```python
EXPECTED_METRIC_KEYS = {
    "price_usd1usdt",
    "price_usd1usdc",
    "exit_usd1usdt_1m",
    "exit_usd1usdc_1m",
    "reserves",
    "multichain_supply",
    "estimated_collateralization",
    "bridged_total",
    "locked_total",
    "bridge_delta",
    "supply_change_24h",
}
```

断言每个直接指标选择 `(metric, scope)` 的最新记录，保留 `value`、`unit`、`quality`、`observed_at`、`collected_at`；退出价格保留 `fully_fillable`。没有记录的 key 值为 `None`，不伪造 0。

- [ ] **Step 2: 写 24 小时变化边界失败测试**

分别验证：

- 基线位于 `now - 24h - 30m` 时，返回 `(current / baseline - 1) * 100`。
- 基线早于 `now - 25h15m` 时返回 `None`。
- 当前值或基线值非正数时返回 `None`。

- [ ] **Step 3: 运行测试并确认失败**

Run: `python -m pytest tests/test_dashboard_data.py -q`

Expected: 缺少 metrics 输出和 24 小时计算而失败。

- [ ] **Step 4: 实现固定指标查询**

在一次固定 `IN (...)` 查询中按 `observed_at DESC, id DESC` 取记录，在 Python 中以 `(metric, scope)` `setdefault` 选择最新值。使用固定映射：

```python
METRIC_KEYS = {
    ("market.mid_price", "USD1USDT"): "price_usd1usdt",
    ("market.mid_price", "USD1USDC"): "price_usd1usdc",
    ("market.sell_1000000_terminal_price", "USD1USDT"): "exit_usd1usdt_1m",
    ("market.sell_1000000_terminal_price", "USD1USDC"): "exit_usd1usdc_1m",
    ("por.reserves", "ethereum"): "reserves",
    ("supply.multichain_total", "global"): "multichain_supply",
    ("supply.estimated_collateralization", "global"): "estimated_collateralization",
    ("supply.bridged_total", "global"): "bridged_total",
    ("bridge.locked_total", "global"): "locked_total",
    ("bridge.issuance_delta", "global"): "bridge_delta",
}
```

再查询最近的 `supply.multichain_total` 历史。基线必须不晚于 `now - 24h`，且与该时间点相差不超过 4500 秒。输出百分比单位 `percent`、质量 `CALCULATED`，时间使用当前记录时间。

- [ ] **Step 5: 运行数据层测试**

Run: `python -m pytest tests/test_dashboard_data.py -q`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add usd1_monitor/dashboard_data.py tests/test_dashboard_data.py
git commit -m "汇总网页仪表盘核心指标"
```

## Task 4: 采集健康与最近事件

**Files:**
- Modify: `usd1_monitor/dashboard_data.py`
- Modify: `tests/test_dashboard_data.py`

- [ ] **Step 1: 写采集健康、告警正文和敏感信息清理失败测试**

写入含有以下错误的 `collector_health.last_error`：

```text
POST https://rpc.example/v3/secret-key?token=hidden failed status=429
```

断言 API 输出包含 `https://rpc.example` 和 `status=429`，但不包含 `secret-key`、`token=hidden` 或数据库绝对路径。健康项同时保留连续失败次数和最近成功/失败时间。

再写入含 `https://docs.example/report?id=123&token=hidden#section` 的告警正文，断言输出保留 `https://docs.example/report`，但删除查询参数、片段和 `token=hidden`。

- [ ] **Step 2: 写最近列表失败测试**

插入 12 条 alert、chain event、announcement，断言各列表只返回最新 10 条。告警 `:part:001`/`:part:002` 必须按基础 alert key 合并为一项；`CANCELLED` 不显示。公告 URL 仅允许 `http`/`https`，非法 scheme 输出 `None`。

- [ ] **Step 3: 运行测试并确认失败**

Run: `python -m pytest tests/test_dashboard_data.py -q`

Expected: 缺少 health/recent 查询和清理逻辑而失败。

- [ ] **Step 4: 实现有界查询和清理函数**

增加纯函数：

```python
def sanitize_dashboard_error(value: str | None) -> str | None: ...
def sanitize_dashboard_text(value: str | None) -> str | None: ...
def safe_external_url(value: object) -> str | None: ...
def base_alert_key(value: str) -> str: ...
```

`sanitize_dashboard_error` 用 URL 正则找到完整 URL，再通过现有 `sanitize_url()` 只保留 scheme/host/port；额外将当前 `database_path` 字符串替换为 `[database]`。输出最多 300 个字符。

`sanitize_dashboard_text` 只处理告警/事件正文中识别出的 HTTP(S) URL：保留 scheme、host、port 和 path，删除 userinfo、query、fragment；无法安全解析的 URL 替换为 `[link]`。所有从 `alert_deliveries.content` 取出的当前告警和最近告警正文都必须经过该函数，避免把 RPC URL 密钥或查询令牌送到浏览器。

最近列表 SQL 都使用参数化 `LIMIT 20` 获取候选，再在 Python 中合并和截断到 10。chain event 只对 Ethereum/BSC 生成 Etherscan/BscScan 交易链接；announcement 使用存储 URL 的安全版本。所有 metadata/payload 都只挑选显示所需字段，不原样返回整个 JSON。

- [ ] **Step 5: 运行数据层测试**

Run: `python -m pytest tests/test_dashboard_data.py -q`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add usd1_monitor/dashboard_data.py tests/test_dashboard_data.py
git commit -m "展示仪表盘健康状态与最近事件"
```

## Task 5: aiohttp 汇总 API 与安全边界

**Files:**
- Create: `usd1_monitor/dashboard_server.py`
- Create: `tests/test_dashboard_server.py`

- [ ] **Step 1: 写 API 成功和 healthz 失败测试**

定义只实现 `snapshot()` 的假仓库，用 `aiohttp.test_utils.TestServer/TestClient` 启动 `create_dashboard_app(fake)`，断言：

```python
response = await client.get("/api/dashboard")
assert response.status == 200
assert await response.json() == SNAPSHOT
assert response.headers["Cache-Control"] == "no-store"

health = await client.get("/healthz")
assert await health.json() == {"status": "ok"}
```

- [ ] **Step 2: 写 503、405 和安全头失败测试**

假仓库抛 `DashboardDataError` 时断言 API 返回：

```python
{
    "error": "dashboard_data_unavailable",
    "message": "监控数据暂时无法读取",
}
```

状态码为 503，响应不包含内部异常。POST `/api/dashboard` 返回 405。所有 HTML/API 响应包含：

```text
Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
Cache-Control: no-store
```

- [ ] **Step 3: 写固定监听地址失败测试**

断言模块常量 `DASHBOARD_HOST == "127.0.0.1"`，`run_dashboard` 构造 `DashboardRepository` 时传入配置的 database/timezone/event window，并把 `config.dashboard.port` 交给 `web.TCPSite`。通过注入 site factory 或 monkeypatch 验证参数，不实际监听公网端口。

- [ ] **Step 4: 运行测试并确认失败**

Run: `python -m pytest tests/test_dashboard_server.py -q`

Expected: 模块和 app factory 尚不存在而失败。

- [ ] **Step 5: 实现 aiohttp 服务**

`create_dashboard_app(repository)` 注册固定 GET 路由。中间件统一设置上述安全头；API 仅捕获 `DashboardDataError` 并记录异常上下文，未预期异常继续由 aiohttp 记录为 500，不增加宽泛吞错。

`run_dashboard(config)`：

1. 创建并打开 `DashboardRepository`。
2. 创建 `web.AppRunner` 和 `web.TCPSite(host=DASHBOARD_HOST, port=config.dashboard.port)`。
3. 启动后等待一个永不自行完成的 `asyncio.Event()`。
4. 在取消或退出时依次 `runner.cleanup()`、`repository.close()`。

静态文件路径使用 `Path(__file__).with_name("dashboard_static")`，请求不存在资源返回 404，不允许客户端拼接任意文件路径。

- [ ] **Step 6: 运行服务层测试**

Run: `python -m pytest tests/test_dashboard_server.py -q`

Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add usd1_monitor/dashboard_server.py tests/test_dashboard_server.py
git commit -m "提供只读网页仪表盘接口"
```

## Task 6: 风险优先响应式页面

**Files:**
- Create: `usd1_monitor/dashboard_static/index.html`
- Create: `usd1_monitor/dashboard_static/dashboard.css`
- Create: `usd1_monitor/dashboard_static/dashboard.js`
- Create: `tests/test_dashboard_assets.py`
- Modify: `tests/test_dashboard_server.py`

- [ ] **Step 1: 写静态资源失败测试**

读取三个资源并断言：

```python
assert '<meta name="viewport"' in html
assert 'id="business-status"' in html
assert 'id="health-status"' in html
assert 'id="active-risks"' in html
assert 'id="key-metrics"' in html
assert 'id="collector-health"' in html
assert 'id="recent-events"' in html
assert "30000" in javascript
assert "textContent" in javascript
assert "innerHTML" not in javascript
assert "@media" in css
assert "https://" not in html + css + javascript
```

服务测试再断言 `/`、CSS、JS 返回正确 content type。

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m pytest tests/test_dashboard_assets.py tests/test_dashboard_server.py -q`

Expected: 静态资源不存在而失败。

- [ ] **Step 3: 实现语义化 HTML**

`index.html` 只引用本地 `/assets/dashboard.css` 和 `/assets/dashboard.js`，结构顺序固定为：

```html
<header>标题、更新时间、手动刷新按钮</header>
<main>
  <section class="status-grid">资产风险、监控健康</section>
  <section id="active-risks">当前提醒</section>
  <section id="key-metrics">关键指标</section>
  <section id="collector-health">数据源健康</section>
  <section id="recent-events">最近事件</section>
</main>
```

首次加载使用“正在读取监控数据”，绝不预填 GREEN。

- [ ] **Step 4: 实现响应式 CSS**

桌面端最大内容宽度 1200px，状态卡两列、指标卡四列；小于 720px 时全部改为单列或两列指标。风险颜色同时配文字和图标，正文对比度不依赖颜色。链接和按钮触控高度至少 44px。

- [ ] **Step 5: 实现 30 秒安全刷新**

JavaScript 必须：

```javascript
const REFRESH_MS = 30000;
```

启动时立即 fetch，随后 `setInterval(refreshDashboard, REFRESH_MS)`；`visibilitychange` 返回可见状态时立即刷新。所有数据库内容通过 `document.createElement()` 和 `textContent` 渲染，链接先验证 `new URL(value).protocol` 为 `http:` 或 `https:`，并设置 `target="_blank"`、`rel="noopener noreferrer"`。

刷新失败时不清空 `lastSnapshot`；存在旧数据则保留内容并显示过期横幅，不存在旧数据则只显示不可用卡片。用 `AbortController` 设置 10 秒请求超时，避免重叠请求。

- [ ] **Step 6: 运行静态资源和服务测试**

Run: `python -m pytest tests/test_dashboard_assets.py tests/test_dashboard_server.py -q`

Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add usd1_monitor/dashboard_static tests/test_dashboard_assets.py tests/test_dashboard_server.py
git commit -m "实现风险优先网页仪表盘"
```

## Task 7: 部署、覆盖声明与操作文档

**Files:**
- Modify: `config.example.yaml`
- Modify: `deploy/config.production.example.yaml`
- Create: `deploy/usd1-dashboard.service`
- Modify: `README.md`
- Modify: `usd1_monitor/scheduler.py`
- Modify: `tests/test_examples.py`
- Modify: `tests/test_status_output.py`

- [ ] **Step 1: 写示例配置和覆盖状态失败测试**

在 `tests/test_examples.py` 断言两个示例配置的 dashboard 端口均为 8080。在 `tests/test_status_output.py` 将期望集合删除 `web_dashboard`，并保留：

```python
assert "web_dashboard" not in NOT_MONITORED
```

- [ ] **Step 2: 写 systemd 安全边界失败测试**

在 `tests/test_examples.py` 读取 `deploy/usd1-dashboard.service`，断言包含：

```text
ExecStart=/opt/usd1-monitor/.venv/bin/python -m usd1_monitor --config /etc/usd1-monitor.yaml dashboard
User=usd1-monitor
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadOnlyPaths=/var/lib/usd1-monitor
```

并断言不包含 `ReadWritePaths=/var/lib/usd1-monitor` 或 `0.0.0.0`。

- [ ] **Step 3: 运行测试并确认失败**

Run: `python -m pytest tests/test_examples.py tests/test_status_output.py -q`

Expected: 服务文件和配置尚未更新、`web_dashboard` 仍为未覆盖而失败。

- [ ] **Step 4: 更新配置和覆盖声明**

在两个 YAML 根级增加：

```yaml
dashboard:
  port: 8080
```

从 `NOT_MONITORED` 删除 `web_dashboard`，不把 dashboard 添加到监控主进程的“正在监控”采集器列表，因为它是独立服务。

- [ ] **Step 5: 增加独立 systemd unit**

沿用主服务的目录、用户和 EnvironmentFile。设置 `Restart=on-failure`、`RestartSec=5`，并将数据库目录声明为 `ReadOnlyPaths`。dashboard 服务不声明日志目录写权限。

- [ ] **Step 6: 更新 README**

增加：

- 第四个 CLI 命令及只读语义。
- dashboard 配置示例。
- 启用 `usd1-dashboard.service` 的命令。
- SSH 隧道命令和本地浏览器地址。
- “固定监听 127.0.0.1，不可直接公网访问”的安全说明。
- 数据库或 API 失败时不显示绿色的故障语义。
- 从未覆盖列表删除 `web_dashboard`。

- [ ] **Step 7: 运行示例和状态测试**

Run: `python -m pytest tests/test_examples.py tests/test_status_output.py tests/test_cli.py -q`

Expected: PASS。

- [ ] **Step 8: 提交**

```bash
git add config.example.yaml deploy/config.production.example.yaml deploy/usd1-dashboard.service README.md usd1_monitor/scheduler.py tests/test_examples.py tests/test_status_output.py
git commit -m "补充网页仪表盘部署说明"
```

## Task 8: 完整验证与手工只读验收

**Files:**
- Modify only if a directly related test exposes a defect.

- [ ] **Step 1: 运行 dashboard 定向测试**

Run:

```bash
python -m pytest tests/test_dashboard_data.py tests/test_dashboard_server.py tests/test_dashboard_assets.py tests/test_config.py tests/test_cli.py tests/test_examples.py tests/test_status_output.py -q
```

Expected: PASS。

- [ ] **Step 2: 运行全量离线测试**

Run: `python -m pytest -q`

Expected: 全部通过，仅保留原有 live skipped。

- [ ] **Step 3: 运行静态和依赖检查**

Run:

```bash
python -m compileall -q usd1_monitor tests
python -m pip check
git diff --check
```

Expected: 三项退出码均为 0。

- [ ] **Step 4: 用临时数据库执行只读 HTTP 验收**

在 `tests/test_dashboard_server.py` 增加 `test_dashboard_http_reads_existing_database_without_mutation`：用现有 `Storage` fixture 写入一组业务 YELLOW、健康 GREEN 和核心指标，关闭可写连接并记录数据库文件哈希；用真实 `DashboardRepository` 启动 `TestServer` 请求 `/api/dashboard`，关闭服务后再次计算哈希，断言响应状态分离且前后哈希相同。

Run:

```bash
python -m pytest tests/test_dashboard_server.py::test_dashboard_http_reads_existing_database_without_mutation -q
```

Expected: PASS，且测试断言状态、数据结构、时间和数据库哈希均符合预期。

- [ ] **Step 5: 浏览器响应式验收**

在 390px 和 1280px 两种视口检查：风险状态位于首屏、无横向滚动、来源链接可点击、刷新失败横幅保留上一屏数据、控制台无错误。

- [ ] **Step 6: 确认只监听本机**

Linux 运行：

```bash
ss -ltnp | grep ':8080'
```

Expected: 监听地址仅为 `127.0.0.1:8080`。

- [ ] **Step 7: 检查最终差异**

Run:

```bash
git status --short
git diff --stat origin/main...HEAD
git log --oneline origin/main..HEAD
```

确认没有数据库、日志、`.env`、视觉草图或无关文件进入提交。
