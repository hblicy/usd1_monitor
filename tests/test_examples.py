from pathlib import Path

from usd1_monitor.config import load_config


HOME_DEPLOYMENT = "/home/ubuntu/usd1_monitor"
LEGACY_DEPLOYMENT_PATHS = (
    "/opt/usd1-monitor",
    "/etc/usd1-monitor",
    "/var/lib/usd1-monitor",
    "/var/log/usd1-monitor",
)


def test_example_config_is_loadable() -> None:
    config = load_config(Path("config.example.yaml"), environ={})

    assert set(config.chains.model_dump()) == {"ethereum", "bsc"}
    assert config.por.interval_seconds == 300
    assert config.information.binance.interval_seconds == 900
    assert config.supply.multichain.tempo_rpc_urls == [
        "https://rpc.presto.tempo.xyz"
    ]


def test_examples_use_ten_minute_permission_monitoring() -> None:
    for path in (
        Path("config.example.yaml"),
        Path("deploy/config.production.example.yaml"),
    ):
        config = load_config(path, environ={})
        for chain in (config.chains.ethereum, config.chains.bsc):
            assert chain.interval_seconds == 600
            assert chain.scan_batch_blocks == 2_000
            assert chain.log_query_chunk_blocks == 500


def test_examples_use_local_dashboard_port() -> None:
    for path in (
        Path("config.example.yaml"),
        Path("deploy/config.production.example.yaml"),
    ):
        assert load_config(path, environ={}).dashboard.port == 8080
        assert "dashboard:\n  port: 8080" in path.read_text(encoding="utf-8")


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


def test_dashboard_systemd_service_is_read_only_and_local() -> None:
    service = Path("deploy/usd1-dashboard.service").read_text(encoding="utf-8")

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
