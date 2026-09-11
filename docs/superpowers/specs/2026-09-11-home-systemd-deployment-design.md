# Home 目录 systemd 部署路径设计

## 目标

将 Linux systemd 部署示例调整为用户实际使用的目录 `/home/ubuntu/usd1_monitor`，由 `ubuntu` 用户运行监控和只读仪表盘服务。配置、环境文件、虚拟环境、SQLite 数据和日志均保存在项目目录内。

## 路径约定

- 项目与工作目录：`/home/ubuntu/usd1_monitor`
- Python：`/home/ubuntu/usd1_monitor/.venv/bin/python`
- 配置：`/home/ubuntu/usd1_monitor/config.yaml`
- 环境变量：`/home/ubuntu/usd1_monitor/.env`
- SQLite：`/home/ubuntu/usd1_monitor/data/monitor.db`
- 日志：`/home/ubuntu/usd1_monitor/logs/usd1-monitor.log`

相对数据和日志路径继续由 `config.yaml` 控制；生产配置模板改为 `data/monitor.db` 和 `logs/usd1-monitor.log`，README 使用同一默认布局。现有服务器上的 `config.yaml` 不会被自动修改。

## systemd 服务

两个服务均设置 `User=ubuntu`、`Group=ubuntu` 和统一的 `WorkingDirectory`、`EnvironmentFile`、`ExecStart` 绝对路径。

保留现有安全限制，但将与 home 目录冲突的 `ProtectHome=true` 改为 `ProtectHome=read-only`：

- 监控服务仅通过 `ReadWritePaths` 获准写入项目下的 `data/` 和 `logs/`。
- 仪表盘服务保持只读，不获得任何项目目录写权限。
- 网页仍只监听 `127.0.0.1`，不改变端口、功能或认证边界。

## 配置、README 与测试

`deploy/config.production.example.yaml` 只调整数据库与日志路径为项目内相对路径。README 改为在 `/home/ubuntu/usd1_monitor` 中创建虚拟环境、复制示例配置和 `.env`、创建 `data/` 与 `logs/`，并以 `ubuntu` 用户执行备份和清理命令。

更新部署示例测试，断言生产模板及两个 unit 使用新的路径、用户和读写边界。除部署路径外，不修改数据库结构、应用配置模型、采集频率或程序行为。

## 验收

- 两个 systemd unit 不再包含 `/opt/usd1-monitor`、`/etc/usd1-monitor*`、`/var/lib/usd1-monitor` 或 `/var/log/usd1-monitor`。
- 生产配置模板使用 `data/monitor.db` 和 `logs/usd1-monitor.log`，不再引用 `/var` 路径。
- 监控服务可写项目内 `data/`、`logs/`；仪表盘只能读取。
- README 命令与 unit 路径一致。
- 部署示例测试和全量测试通过。
