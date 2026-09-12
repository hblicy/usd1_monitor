from datetime import date
from pathlib import Path

from usd1_monitor.config import load_config


HOME_DEPLOYMENT = "/home/ubuntu/usd1_monitor"
LEGACY_DEPLOYMENT_PATHS = (
    "/opt/usd1-monitor",
    "/etc/usd1-monitor",
    "/var/lib/usd1-monitor",
    "/var/log/usd1-monitor",
)

EXPECTED_EVM_ADDRESSES = {
    "0xf977814e90da44bfa03b6295a0616a897441acec",
    "0x5a52e96bacdabb82fd05763e25335261b270efcb",
    "0x28c6c06298d514db089934071355e5743bf21d60",
    "0x47ac0fb4f2d84898e4d9e7b4dab3c24507a6d503",
    "0x21a31ee1afc51d94c2efccaa2092ad1028285549",
    "0xdfd5293d8e347dfe59e90efd55b2956a1343963d",
    "0xbe0eb53f46cd790cd13851d5eff43d12404d33e8",
    "0x8894e0a0c962cb723c1976a4421c95949be2d4e3",
    "0xe2fc31f816a9b94326492132018c3aecc4a93ae1",
    "0x9696f59e4d72e237be84ffd425dcad154bf96976",
    "0x56eddb7aa87536c09ccc2793473599fd21a8b17f",
    "0x4976a4a02f38326660d17bf34b431dc6e2eb2327",
    "0x01c952174c24e1210d26961d456a77a39e1f0bb0",
}
EXPECTED_SOLANA_BINANCE = {
    "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9",
    "2ojv9BAiHUrvsm9gxDe7fJSzbNZSJcxZvf8dqmWGHG8S",
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",
    "3yFwqXBfZY4jBVUafQ1YEXw189y2dN3V5KQq9uzBDy1E",
    "3gd3dqgtJ4jWfBfLYTX67DALFetjc5iS72sCgRhCkW2u",
    "6QJzieMYfp7yr3EdrePaQoG3Ghxs2wM98xSLRu8Xh56U",
}
EXPECTED_SOLANA_FIREBLOCKS = "9Rycov3U4efJf5HiqZYGjN7qJJHEtMsj4vbmkG4xfCxk"
EXPECTED_WHALES = {
    "0xAC3E216bD55860912062a4027A03b99587B7FfC7",
    "0x041c32c919de3e85e0D89984c2590434f6569dFA",
}
BINANCE_EVIDENCE_URL = "https://www.binance.com/en/square/post/97671"


def test_example_config_is_loadable() -> None:
    config = load_config(Path("config.example.yaml"), environ={})

    assert set(config.chains.model_dump()) == {"ethereum", "bsc"}
    assert config.por.interval_seconds == 300
    assert config.information.binance.interval_seconds == 900
    assert config.supply.multichain.tempo_rpc_urls == [
        "https://rpc.presto.tempo.xyz"
    ]


def test_core_risk_examples_have_explicit_address_inventory_without_credentials() -> None:
    expected_addresses = 37
    expected_statuses = {"trusted": 26, "candidate": 11}
    expected_entities = {
        "binance_cex": 30,
        "binance_peg_reserve": 2,
        "fireblocks_custody": 1,
        "unlabeled_whale": 4,
    }

    for path in (
        Path("config.example.yaml"),
        Path("deploy/config.production.example.yaml"),
    ):
        config = load_config(path, environ={})
        addresses = config.custody.addresses

        assert len(addresses) == expected_addresses
        assert {
            status: sum(item.status == status for item in addresses)
            for status in expected_statuses
        } == expected_statuses
        assert {
            entity: sum(item.entity == entity for item in addresses)
            for entity in expected_entities
        } == expected_entities
        assert all(item.status == "candidate" for item in addresses if item.chain == "solana")
        assert all(
            "${" not in url
            and "token" not in url.casefold()
            and "secret" not in url.casefold()
            for values in config.redemption.model_dump().values()
            if isinstance(values, list)
            for url in values
            if isinstance(url, str)
        )


def test_core_risk_examples_lock_the_exact_address_inventory() -> None:
    for path in (
        Path("config.example.yaml"),
        Path("deploy/config.production.example.yaml"),
    ):
        config = load_config(path, environ={})
        addresses = config.custody.addresses
        evm = [item for item in addresses if item.chain in {"ethereum", "bsc"}]
        assert {item.address.lower() for item in evm if item.entity != "unlabeled_whale"} == (
            EXPECTED_EVM_ADDRESSES
        )
        assert {
            item.address
            for item in addresses
            if item.chain == "solana" and item.entity == "binance_cex"
        } == EXPECTED_SOLANA_BINANCE
        assert {
            item.address
            for item in addresses
            if item.chain == "solana" and item.entity == "fireblocks_custody"
        } == {EXPECTED_SOLANA_FIREBLOCKS}
        assert {
            item.address
            for item in addresses
            if item.entity == "unlabeled_whale"
        } == EXPECTED_WHALES

        for item in evm:
            if item.entity == "unlabeled_whale":
                assert item.status == "candidate"
                assert item.role == "whale"
                continue
            assert item.status == "trusted"
            assert item.verified_on == date(2026, 9, 11)
            assert item.evidence[0].kind == "official"
            assert item.evidence[0].url == BINANCE_EVIDENCE_URL
            if item.address.lower() == "0x47ac0fb4f2d84898e4d9e7b4dab3c24507a6d503":
                assert item.entity == "binance_peg_reserve"
                assert item.role == "reserve"
            elif item.address.lower() == "0xbe0eb53f46cd790cd13851d5eff43d12404d33e8":
                assert item.entity == "binance_cex"
                assert item.role == "cold_wallet"
            else:
                assert item.entity == "binance_cex"
                assert item.role == "hot_wallet"

        for item in addresses:
            if item.chain == "solana":
                assert item.status == "candidate"
                assert item.verified_on is None
                assert len(item.evidence) == 1
                assert item.evidence[0].kind == "label"
            if item.entity == "unlabeled_whale":
                assert item.chain in {"ethereum", "bsc"}
                assert item.status == "candidate"


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


def test_readme_documents_core_risk_boundaries_and_rpc_budget() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")

    for required in (
        "官方来源明确列出，或两个相互独立的公开标签来源一致",
        "90 天",
        "candidate 仅展示",
        "已核验地址下限",
        "Solana 只计算净余额差",
        "por.coverage_max_age_seconds",
        "达到 30 分钟（默认 1800 秒）即",
        "覆盖率为 UNKNOWN",
        "por.yellow_staleness_seconds",
        "por.red_staleness_seconds",
        "用途不同",
        "未发现官方限制",
        "不等于主动赎回成功",
        "媒体线索不告警",
        "EVM：约 13.82–14.28 万次/月",
        "Solana：约 3.02–3.12 万次/月",
        "合计约 16.85–17.41 万次/月",
        "命中可信 EVM 地址的唯一 Transfer 区块数",
        "每个相关区块只补取一次时间戳",
    ):
        assert required in readme
