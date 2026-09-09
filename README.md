# USD1 公开数据风险监控器

这是一个只读监控程序：不需要交易所 API Key 或钱包私钥，不下单、不兑换、不执行资产操作。它监控 Binance 公开盘口、Ethereum/BNB Chain 合约与供应、USD1 PoR Oracle、DefiLlama 估算全链供应，以及 Binance、BitGo、WLFI、OCC 官方页面；状态变化和恢复可通过企业微信机器人通知。

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

程序会自动读取 `config.yaml` 同目录的 `.env`，且不会覆盖 shell 或 systemd 已显式设置的环境变量。观测数据默认保留 180 天，可通过 `retention_days` 调整。

```dotenv
WECHAT_WEBHOOK=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...
ETH_RPC_URLS=https://eth-primary.example,https://eth-backup.example
BSC_RPC_URLS=https://bsc-primary.example,https://bsc-backup.example
```

Linux 上执行 `chmod 600 .env` 或 `/etc/usd1-monitor.env`。不要提交 `.env`、真实 webhook、带密钥的 RPC URL、数据库或日志。

三个命令：

```bash
python -m usd1_monitor --config config.yaml check
python -m usd1_monitor --config config.yaml run
python -m usd1_monitor --config config.yaml status
```

- `check`：只读执行一次全部采集，不发送企业微信；所有启用的采集器成功才返回 0。
- `run`：常驻调度。未配置 webhook 时仍运行，但日志明确显示通知已禁用。
- `status`：打开并兼容初始化 SQLite（不采集外部数据、不发送通知），显示业务风险、采集器健康、最近链上事实、官方信息和未监控项。

## 告警语义

- `GREEN → YELLOW/RED`、`YELLOW → RED`：通知一次。
- 同等级持续：不重复通知。
- `RED → YELLOW/GREEN`、`YELLOW → GREEN`：满足恢复条件后通知。
- 采集器连续失败 3 次为黄色监控盲区；Binance 盘口或 PoR 在已有成功记录后超过 15 分钟无成功观测为红色盲区。
- 企业微信 HTTP 200 仍检查业务 `errcode`；失败最多尝试两次，未成功不会标记为已送达。
- `FACT` 是直接链上/交易所事实；`ESTIMATED_SOURCE` 是外部估算来源；`ESTIMATED` 是 `PoR reserves / DefiLlama global supply` 的辅助比率，不是完整多链审计结论。

## 数据源与边界

公开数据源：Binance Spot REST、Ethereum/BNB Chain JSON-RPC、WLFI PoR Oracle、DefiLlama stablecoins API、Binance/BitGo/WLFI/OCC 官方页面。

以下能力会在 `status` 中明确列为 `NOT_MONITORED`：

- `private_exchange_account`
- `active_conversion_probe`
- `tron_solana_aptos_tempo_bridges`
- `binance_wallet_concentration`
- `social_media_sentiment`
- `defi_liquidations`
- `web_dashboard`
- `full_multichain_supply_reconciliation`

它们不应被解释为绿色或已覆盖。政治/社交信号保留人工判断，不自动触发交易动作。

## Linux systemd 部署

生产配置模板为 `deploy/config.production.example.yaml`，服务单元为 `deploy/usd1-monitor.service`。

```bash
sudo useradd --system --home /opt/usd1-monitor --shell /usr/sbin/nologin usd1-monitor
sudo mkdir -p /opt/usd1-monitor /var/lib/usd1-monitor /var/log/usd1-monitor
sudo chown -R usd1-monitor:usd1-monitor /opt/usd1-monitor /var/lib/usd1-monitor /var/log/usd1-monitor
# 将程序复制到 /opt/usd1-monitor 后：
sudo -u usd1-monitor python3 -m venv /opt/usd1-monitor/.venv
sudo -u usd1-monitor /opt/usd1-monitor/.venv/bin/pip install -r /opt/usd1-monitor/requirements.txt
sudo cp deploy/config.production.example.yaml /etc/usd1-monitor.yaml
sudo cp .env.example /etc/usd1-monitor.env
sudo chmod 600 /etc/usd1-monitor.env
sudo cp deploy/usd1-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now usd1-monitor
sudo systemctl status usd1-monitor
sudo journalctl -u usd1-monitor -f
```

应用日志默认在 `/var/log/usd1-monitor/monitor.log`，SQLite 在 `/var/lib/usd1-monitor/monitor.db`。数据库在线备份使用 SQLite 自带命令，避免直接复制 WAL 中的活跃数据库：

```bash
sudo -u usd1-monitor sqlite3 /var/lib/usd1-monitor/monitor.db ".backup '/var/lib/usd1-monitor/monitor-backup.db'"
```

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
