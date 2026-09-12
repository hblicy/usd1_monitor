# USD1 公开数据风险监控器

这是一个只读监控程序：不需要交易所 API Key 或钱包私钥，不下单、不兑换、不执行资产操作。它监控 Binance 公开盘口、Ethereum/BNB Chain 合约权限、USD1 PoR Oracle、完整多链供应量与桥池余额、DefiLlama 辅助估算，以及 Binance、BitGo、WLFI、OCC 官方页面；状态变化和恢复可通过企业微信机器人通知。

## 本地运行

要求 Python 3.11+（推荐 3.12）。

```bash
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows PowerShell
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
cp config.example.yaml config.yaml
cp .env.example .env
```

Windows 可用 `Copy-Item` 代替 `cp`。按部署环境修改 `config.yaml`，尤其是 RPC URL 和 `watched_addresses`。`.env` 只保存敏感/环境值：

已有部署升级后，需要在实际 `config.yaml` 的 `chains.ethereum` 和 `chains.bsc` 下都设置 `interval_seconds: 600`、`scan_batch_blocks: 2000`、`log_query_chunk_blocks: 500`，并从 `deploy/config.production.example.yaml` 复制完整的 `custody:` 配置段；不要用模板覆盖包含实际 RPC 等环境设置的配置文件。然后重启服务。程序不会自动覆盖实际配置文件。

如果现有 `redemption.official_page_urls` 包含持续返回 HTTP 403 的 BitGo Investor News 页面，请删除 `https://investors.bitgo.com/news/default.aspx`；其余 BitGo Status、USD1、USD1 Terms 和 WLFI FAQ 数据源继续作为必需检查项。

程序会自动读取 `config.yaml` 同目录的 `.env`，且不会覆盖 shell 或 systemd 已显式设置的环境变量。观测数据默认保留 180 天，可通过 `retention_days` 调整。

```dotenv
WECHAT_WEBHOOK=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...
ETH_RPC_URLS=https://eth-primary.example,https://eth-backup.example
BSC_RPC_URLS=https://bsc-primary.example,https://bsc-backup.example
# 其他链也可使用逗号分隔的 *_RPC_URLS 覆盖，详见 .env.example
```

Linux 上执行 `chmod 600 /home/ubuntu/usd1_monitor/.env`。不要提交 `.env`、真实 webhook、带密钥的 RPC URL、数据库或日志。

四个命令：

```bash
python -m usd1_monitor --config config.yaml check
python -m usd1_monitor --config config.yaml run
python -m usd1_monitor --config config.yaml status
python -m usd1_monitor --config config.yaml dashboard
```

- `check`：只读执行一次全部采集，不发送企业微信；所有必需采集器成功才返回 0，DefiLlama 仅为辅助来源。
- `run`：常驻调度。未配置 webhook 时仍运行，但日志明确显示通知已禁用。
- `status`：打开并兼容初始化 SQLite（不采集外部数据、不发送通知），显示业务风险、采集器健康、最近链上事实、官方信息和未监控项。
- `dashboard`：以只读方式打开已有 SQLite，在 `127.0.0.1:8080` 提供网页总览；不采集外部数据、不发送通知、不会创建或迁移数据库。

网页端口可在配置文件中修改，但监听地址固定为本机：

```yaml
dashboard:
  port: 8080
```

首次读取失败时，网页显示“未知”，不会把缺少数据解释为绿色；后续刷新失败会保留上一屏成功数据并明确提示内容可能过期。

## 告警语义

- `GREEN → YELLOW/RED`、`YELLOW → RED`：通知一次。
- 同等级持续：不重复通知。
- `RED → YELLOW/GREEN`、`YELLOW → GREEN`：满足恢复条件后通知。
- 企业微信使用简明中文纯文本，状态显示为“正常”、“注意”或“危险”，不显示内部规则编号、原因哈希和原始 RPC 报错。
- 采集器问题单独显示为“监控异常”或“监控已恢复”，不与 USD1 业务风险混淆；失败后需稳定 60 秒才恢复为绿色。
- 微信仅显示人工判断所需的摘要、关键数值、时间、来源和建议；完整技术诊断仍保留在日志和 SQLite 中。
- 官方信息首次采集和超过 24 小时的历史公告只建立基线；之后的新公告或正文变化才参与风险判断。
- 官方信息事件超过观察窗口时静默转为绿色，不发送逐条“event window expired”通知；真正的规则恢复仍通知。
- 采集器连续失败 3 次为黄色监控盲区；Binance 盘口在已有成功记录后超过 15 分钟无成功观测为红色盲区。PoR 数据过期等级分别由 `por.yellow_staleness_seconds` 和 `por.red_staleness_seconds` 配置，默认超过 1 小时为黄色、超过 2 小时为红色。
- 企业微信 HTTP 200 仍检查业务 `errcode`；失败最多尝试两次，未成功不会标记为已送达。
- `FACT` 是直接链上/交易所事实；`ESTIMATED_SOURCE` 是外部估算来源；`ESTIMATED` 是 `PoR reserves / supply.multichain_total` 的覆盖率估算，不是审计结论。

## 数据源与边界

公开数据源：Binance Spot REST、各链 JSON-RPC/公开索引器、WLFI PoR Oracle、DefiLlama stablecoins API、Binance/BitGo/WLFI/OCC 官方页面。

完整供应量每小时核对一次：原生供应量包含 Ethereum、BNB Chain、Tron、Solana、Aptos、Tempo；桥接发行量包含 Plume、AB Core、Monad、Mantle、Morph；同时核对 Ethereum、BNB Chain、Solana、Aptos、Tempo 的 CCIP 桥池余额。总供应量只汇总 6 条原生链，避免把桥接发行重复计算。正常一轮少于 30 个 RPC/HTTP 响应，不使用 `eth_getLogs`、trace 或 debug 方法。

核心资产风险信号采用以下边界：

- 可信托管地址必须由所属实体的官方来源明确列出，或两个相互独立的公开标签来源一致；人工核验有效期为 90 天。candidate 仅展示，不参与集中度、资金流阈值或资产总状态。
- Binance 集中度只统计仍在核验有效期内的 `binance_cex` 与 `binance_peg_reserve` 地址，因此显示为“已核验地址下限”，不代表 Binance 的完整持仓。
- Ethereum、BNB Chain 资金流复用已有 USD1 Transfer 事件；Solana 只计算净余额差，不推断交易对手。
- 储备覆盖率新鲜度由 `por.coverage_max_age_seconds` 配置；PoR 数据年龄达到 30 分钟（默认 1800 秒）即覆盖率为 UNKNOWN。它与用于监控数据过期等级的 `por.yellow_staleness_seconds`、`por.red_staleness_seconds` 用途不同。完整多链供应量过期时覆盖率同样为 UNKNOWN；最后一次储备金额仍可展示，但不能继续支撑绿色判断。
- 官方页面和 BitGo Status 没有发现异常时只表示“未发现官方限制”，不等于主动赎回成功。本程序不使用账户或钱包做真实赎回测试。
- 媒体线索不告警，只在网页仪表盘作为待核实信息展示，不改变资产风险状态，也不发送企业微信。

按默认 10 分钟检查频率和当前示例地址数量估算，托管地址监控的固定 RPC 响应基线为：EVM：约 13.82–14.28 万次/月；Solana：约 3.02–3.12 万次/月；合计约 16.85–17.41 万次/月（分别按 30/31 天计算）。EVM 资金流另加命中可信 EVM 地址的唯一 Transfer 区块数；每个相关区块只补取一次时间戳，不重复拉取日志，也不恢复逐区块交易扫描。这部分变量增量应以上线后的实际命中区块数核对。

`TRON_RPC_URLS`、`SOLANA_RPC_URLS`、`APTOS_INDEXER_URLS`、`TEMPO_RPC_URLS`、`PLUME_RPC_URLS`、`AB_RPC_URLS`、`MONAD_RPC_URLS`、`MANTLE_RPC_URLS`、`MORPH_RPC_URLS` 都支持在 `.env` 中用英文逗号配置多个端点。

以下能力会在 `status` 中明确列为 `NOT_MONITORED`：

- `private_exchange_account`
- `active_conversion_probe`
- `tron_solana_aptos_tempo_bridges`
- `social_media_sentiment`
- `defi_liquidations`

这些未覆盖项不应被解释为绿色。`tron_solana_aptos_tempo_bridges` 指逐笔跨链交易追踪；本程序目前只核对供应量和桥池余额。政治/社交信号保留人工判断，不自动触发交易动作。

## Linux systemd 部署

生产配置模板为 `deploy/config.production.example.yaml`，监控服务单元为 `deploy/usd1-monitor.service`，只读网页服务单元为 `deploy/usd1-dashboard.service`。

以下命令假定仓库已经位于 `/home/ubuntu/usd1_monitor`。`cp -n` 不会覆盖已有的 `config.yaml` 和 `.env`；首次部署复制完成后，请先填写这两个文件再启动服务。

```bash
cd /home/ubuntu/usd1_monitor
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp -n deploy/config.production.example.yaml config.yaml
cp -n .env.example .env
chmod 600 .env
mkdir -p data logs
sudo cp deploy/usd1-monitor.service /etc/systemd/system/
sudo cp deploy/usd1-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now usd1-monitor
sudo systemctl enable --now usd1-dashboard
sudo systemctl status usd1-monitor
sudo systemctl status usd1-dashboard
sudo journalctl -u usd1-monitor -f
```

网页服务固定监听 `127.0.0.1`，不能直接从公网访问，也不提供登录功能。在自己的电脑建立 SSH 隧道：

```bash
ssh -L 8080:127.0.0.1:8080 ubuntu@服务器IP
```

保持该 SSH 会话打开，然后在本机浏览器访问 `http://127.0.0.1:8080`。若修改了 `dashboard.port`，隧道命令两处端口和浏览器地址需要同步修改。网页服务只读数据库，停止或重启它不会影响监控采集和企业微信通知。

应用日志默认在 `/home/ubuntu/usd1_monitor/logs/usd1-monitor.log`，SQLite 在 `/home/ubuntu/usd1_monitor/data/monitor.db`。数据库在线备份使用 SQLite 自带命令，避免直接复制 WAL 中的活跃数据库：

```bash
sqlite3 /home/ubuntu/usd1_monitor/data/monitor.db ".backup '/home/ubuntu/usd1_monitor/data/monitor-backup.db'"
```

如果旧版本已经产生大量官方公告误报，部署本修复时可一次性清理尚未发送的对应队列。必须先停止旧进程，先预览再删除；以下操作不影响已发送历史、风险状态或其他类型告警：

```bash
sudo systemctl stop usd1-monitor
sqlite3 /home/ubuntu/usd1_monitor/data/monitor.db ".backup '/home/ubuntu/usd1_monitor/data/monitor-before-information-alert-fix-20260909.db'"
sqlite3 /home/ubuntu/usd1_monitor/data/monitor.db "SELECT id,status,alert_key FROM alert_deliveries WHERE delivered_at IS NULL AND status IN ('PENDING','IN_FLIGHT') AND (alert_key LIKE 'official:%' OR alert_key LIKE 'expiry:event.information.%') ORDER BY id;"
sqlite3 /home/ubuntu/usd1_monitor/data/monitor.db "BEGIN IMMEDIATE; DELETE FROM alert_deliveries WHERE delivered_at IS NULL AND status IN ('PENDING','IN_FLIGHT') AND (alert_key LIKE 'official:%' OR alert_key LIKE 'expiry:event.information.%'); COMMIT;"
sudo systemctl start usd1-monitor
```

源码目录直接运行时，将数据库路径替换为 `config.yaml` 的 `database_path`（例如 `data/monitor.db`），并确保 `run` 进程已经停止。

## 排障

- Binance 区域限制：`check` 会返回非零并显示 HTTP 错误；部署到允许访问官方 Spot API 的地区，不把失败跳过为成功。
- RPC 失败：配置至少两个 HTTPS RPC，检查 chain ID、限流、归档能力和 URL 密钥；日志会移除 query string。
- 官方页面解析失败：这是监控盲区，不代表“没有新公告”；确认页面结构、地区页面或机器人拦截后更新对应 parser。
- PoR 失败：核对 Ethereum RPC、Oracle 地址、同区块三个调用和 timestamp 一致性。
- 未收到通知：先检查 `WECHAT_WEBHOOK` 是否加载，再看 `alert_deliveries` 的 `attempts/last_error`；`check` 按设计永不发送 webhook。

## 测试

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -v
```

离线测试不访问网络。公开端点冒烟测试必须显式设置 `USD1_RUN_LIVE_TESTS=1`，且只读、不发 webhook。

```bash
# Linux/macOS
USD1_RUN_LIVE_TESTS=1 python -m pytest tests/live -v
# Windows PowerShell
$env:USD1_RUN_LIVE_TESTS='1'; python -m pytest tests/live -v
```
