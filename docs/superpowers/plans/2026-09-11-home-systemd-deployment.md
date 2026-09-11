# Home 目录 systemd 部署 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将监控服务和只读仪表盘的 Linux systemd 部署示例统一到 `/home/ubuntu/usd1_monitor`，并由 `ubuntu` 用户安全运行。

**Architecture:** 应用行为不变，只调整生产配置模板、两个 systemd unit、README 命令和静态部署测试。监控服务在 `ProtectHome=read-only` 下仅获准写项目内 `data/`、`logs/`；仪表盘不获得写权限。

**Tech Stack:** systemd、YAML、Markdown、pytest。

---

## Task 1：锁定 Home 目录部署契约

**Files:**

- Modify: `tests/test_examples.py`

- [ ] **Step 1: 写入生产模板与监控服务失败测试**

在 `tests/test_examples.py` 增加：

```python
HOME_DEPLOYMENT = "/home/ubuntu/usd1_monitor"
LEGACY_DEPLOYMENT_PATHS = (
    "/opt/usd1-monitor",
    "/etc/usd1-monitor",
    "/var/lib/usd1-monitor",
    "/var/log/usd1-monitor",
)


def test_production_example_uses_project_state_paths() -> None:
    config = Path("deploy/config.production.example.yaml").read_text(
        encoding="utf-8"
    )

    assert "database_path: data/monitor.db" in config
    assert "  path: logs/usd1-monitor.log" in config
    assert "/var/lib/usd1-monitor" not in config
    assert "/var/log/usd1-monitor" not in config


def test_monitor_systemd_service_uses_home_deployment() -> None:
    service = Path("deploy/usd1-monitor.service").read_text(encoding="utf-8")

    for required in (
        "User=ubuntu",
        "Group=ubuntu",
        f"WorkingDirectory={HOME_DEPLOYMENT}",
        f"EnvironmentFile={HOME_DEPLOYMENT}/.env",
        f"ExecStart={HOME_DEPLOYMENT}/.venv/bin/python -m usd1_monitor "
        f"--config {HOME_DEPLOYMENT}/config.yaml run",
        "ProtectHome=read-only",
        f"ReadWritePaths={HOME_DEPLOYMENT}/data {HOME_DEPLOYMENT}/logs",
    ):
        assert required in service
    for legacy_path in LEGACY_DEPLOYMENT_PATHS:
        assert legacy_path not in service
```

- [ ] **Step 2: 更新仪表盘服务测试中的部署契约**

将 `test_dashboard_systemd_service_is_read_only_and_local()` 的关键断言改为：

```python
for required in (
    "User=ubuntu",
    "Group=ubuntu",
    f"WorkingDirectory={HOME_DEPLOYMENT}",
    f"EnvironmentFile={HOME_DEPLOYMENT}/.env",
    f"ExecStart={HOME_DEPLOYMENT}/.venv/bin/python -m usd1_monitor "
    f"--config {HOME_DEPLOYMENT}/config.yaml dashboard",
    "NoNewPrivileges=true",
    "ProtectSystem=strict",
    "ProtectHome=read-only",
    f"ReadOnlyPaths={HOME_DEPLOYMENT}",
):
    assert required in service
assert "ReadWritePaths=" not in service
for legacy_path in LEGACY_DEPLOYMENT_PATHS:
    assert legacy_path not in service
assert "0.0.0.0" not in service
```

- [ ] **Step 3: 运行部署示例测试，确认旧配置失败**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_examples.py -q
```

Expected: 新增或更新的生产模板、用户、工作目录、配置路径和 `ProtectHome` 断言失败。

## Task 2：调整生产模板和 systemd unit

**Files:**

- Modify: `deploy/config.production.example.yaml`
- Modify: `deploy/usd1-monitor.service`
- Modify: `deploy/usd1-dashboard.service`
- Test: `tests/test_examples.py`

- [ ] **Step 1: 将生产模板的数据和日志改为相对路径**

在 `deploy/config.production.example.yaml` 设置：

```yaml
database_path: data/monitor.db
```

以及：

```yaml
logging:
  path: logs/usd1-monitor.log
```

不修改其他配置值。

- [ ] **Step 2: 更新监控服务 unit**

将 `deploy/usd1-monitor.service` 的相关行改为：

```ini
User=ubuntu
Group=ubuntu
WorkingDirectory=/home/ubuntu/usd1_monitor
EnvironmentFile=/home/ubuntu/usd1_monitor/.env
ExecStart=/home/ubuntu/usd1_monitor/.venv/bin/python -m usd1_monitor --config /home/ubuntu/usd1_monitor/config.yaml run
```

保留其他安全设置和重启策略，只把 home 保护与写路径改为：

```ini
ProtectHome=read-only
ReadWritePaths=/home/ubuntu/usd1_monitor/data /home/ubuntu/usd1_monitor/logs
```

- [ ] **Step 3: 更新只读仪表盘 unit**

将 `deploy/usd1-dashboard.service` 的相关行改为：

```ini
User=ubuntu
Group=ubuntu
WorkingDirectory=/home/ubuntu/usd1_monitor
EnvironmentFile=/home/ubuntu/usd1_monitor/.env
ExecStart=/home/ubuntu/usd1_monitor/.venv/bin/python -m usd1_monitor --config /home/ubuntu/usd1_monitor/config.yaml dashboard
```

保留其他安全设置和重启策略，只把 home 保护与显式只读路径改为：

```ini
ProtectHome=read-only
ReadOnlyPaths=/home/ubuntu/usd1_monitor
```

不得增加任何 `ReadWritePaths`。

- [ ] **Step 4: 运行部署示例测试**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_examples.py -q
```

Expected: PASS；生产模板使用相对状态路径，监控服务仅可写 `data/`、`logs/`，仪表盘没有写权限。

- [ ] **Step 5: 提交 unit、模板和测试**

```powershell
git add deploy/config.production.example.yaml deploy/usd1-monitor.service deploy/usd1-dashboard.service tests/test_examples.py
git commit -m "调整 systemd Home 目录路径"
```

## Task 3：更新 Linux 部署说明

**Files:**

- Modify: `README.md`
- Modify: `tests/test_examples.py`

- [ ] **Step 1: 写入 README 路径一致性失败测试**

在 `tests/test_examples.py` 增加：

```python
def test_readme_systemd_commands_use_home_deployment() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")

    for required in (
        "cd /home/ubuntu/usd1_monitor",
        "python3 -m venv .venv",
        "cp -n deploy/config.production.example.yaml config.yaml",
        "cp -n .env.example .env",
        "mkdir -p data logs",
        "/home/ubuntu/usd1_monitor/data/monitor.db",
        "/home/ubuntu/usd1_monitor/logs/usd1-monitor.log",
    ):
        assert required in readme
    for legacy_path in LEGACY_DEPLOYMENT_PATHS:
        assert legacy_path not in readme
    assert "sudo -u usd1-monitor" not in readme
```

- [ ] **Step 2: 运行新测试，确认 README 仍使用旧路径**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_examples.py::test_readme_systemd_commands_use_home_deployment -q
```

Expected: FAIL，README 尚未包含 home 目录初始化命令且仍引用 `/opt`、`/etc`、`/var`。

- [ ] **Step 3: 更新首次部署命令**

将 README 的 systemd 部署代码块改为：

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

在代码块前说明命令假定仓库已经位于 `/home/ubuntu/usd1_monitor`，`cp -n` 不覆盖已有 `config.yaml` 和 `.env`，首次部署后必须填写这两个文件。

- [ ] **Step 4: 更新状态路径、备份和队列清理命令**

将正文路径改为：

```text
应用日志默认在 /home/ubuntu/usd1_monitor/logs/usd1-monitor.log，SQLite 在 /home/ubuntu/usd1_monitor/data/monitor.db。
```

数据库备份命令改为：

```bash
sqlite3 /home/ubuntu/usd1_monitor/data/monitor.db ".backup '/home/ubuntu/usd1_monitor/data/monitor-backup.db'"
```

队列清理段落中的三条 SQLite 命令都直接使用 `/home/ubuntu/usd1_monitor/data/monitor.db`，备份文件使用同目录；删除全部 `sudo -u usd1-monitor`。保留“先停止、预览、备份、再删除、最后启动”的既有顺序和 SQL，不改清理范围。

- [ ] **Step 5: 运行部署说明测试**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_examples.py -q
```

Expected: PASS；README、生产模板和两个 unit 的路径一致。

- [ ] **Step 6: 提交 README 与测试**

```powershell
git add README.md tests/test_examples.py
git commit -m "更新 Home 目录部署说明"
```

## Task 4：全量验证并更新 PR

**Files:**

- Verify only; production changes are not expected.

- [ ] **Step 1: 运行全量测试**

Run:

```powershell
.venv\Scripts\python.exe -m pytest -q
```

Expected: 全部 PASS，除既有 live tests 跳过外无失败。

- [ ] **Step 2: 运行配置加载、语法和补丁检查**

Run:

```powershell
.venv\Scripts\python.exe -m compileall -q usd1_monitor tests
.venv\Scripts\python.exe -m pip check
git diff --check
```

Expected: 三条命令退出码均为 0；`pip check` 输出 `No broken requirements found.`。

- [ ] **Step 3: 核对旧部署路径已清除**

Run:

```powershell
rg -n "/opt/usd1-monitor|/etc/usd1-monitor|/var/lib/usd1-monitor|/var/log/usd1-monitor|User=usd1-monitor|Group=usd1-monitor|ProtectHome=true" README.md deploy
```

Expected: 无匹配。测试中的旧路径拒绝清单、历史设计和旧实施计划不在本次清理范围内。

- [ ] **Step 4: 核对最终范围**

Run:

```powershell
git status --short
git diff --stat origin/codex/web-dashboard..HEAD
```

Expected: 工作区干净；新增设计与计划，运行改动仅包含生产配置模板、两个 unit、README 和部署示例测试。

- [ ] **Step 5: 推送并更新 PR #9**

```powershell
git push origin codex/web-dashboard
```

在 PR #9 的中文正文中补充 Home 目录部署路径变更和上述验证结果；不创建新 PR，不修改 `main`。
