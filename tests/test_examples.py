from pathlib import Path

from usd1_monitor.config import load_config


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


def test_dashboard_systemd_service_is_read_only_and_local() -> None:
    service = Path("deploy/usd1-dashboard.service").read_text(encoding="utf-8")

    for required in (
        "ExecStart=/opt/usd1-monitor/.venv/bin/python -m usd1_monitor --config /etc/usd1-monitor.yaml dashboard",
        "User=usd1-monitor",
        "NoNewPrivileges=true",
        "ProtectSystem=strict",
        "ProtectHome=true",
        "ReadOnlyPaths=/var/lib/usd1-monitor",
    ):
        assert required in service
    assert "ReadWritePaths=/var/lib/usd1-monitor" not in service
    assert "0.0.0.0" not in service
