from pathlib import Path

import pytest

from usd1_monitor.config import ConfigError, load_config


def test_load_config_reads_market_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
database_path: data/monitor.db
timezone: Asia/Shanghai
market:
  symbols: [USD1USDT, USD1USDC]
  interval_seconds: 60
  depth_limit: 1000
  yellow_price: 0.997
  yellow_seconds: 900
  red_price: 0.995
  red_seconds: 300
  recovery_price: 0.998
  recovery_seconds: 900
  sell_sizes: [1000000, 5000000, 20000000]
http:
  timeout_seconds: 10
  retries: 2
""".strip(),
        encoding="utf-8",
    )

    config = load_config(path, environ={})

    assert config.market.symbols == ["USD1USDT", "USD1USDC"]
    assert config.market.depth_limit == 1000
    assert config.market.severe_price == 0.99
    assert config.market.severe_seconds == 3600
    assert config.wechat_webhook is None


def test_load_config_rejects_inverted_market_thresholds(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
database_path: monitor.db
market:
  symbols: [USD1USDT, USD1USDC]
  yellow_price: 0.995
  red_price: 0.997
  recovery_price: 0.998
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="red_price must be lower"):
        load_config(path, environ={})


def test_load_config_rejects_severe_threshold_not_below_red(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "database_path: monitor.db\nmarket:\n  severe_price: 0.995\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="severe_price must be lower"):
        load_config(path, environ={})


def test_load_config_requires_all_exit_capacity_sizes(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "database_path: monitor.db\nmarket:\n  sell_sizes: [1000000]\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="1000000, 5000000, and 20000000"):
        load_config(path, environ={})


def test_load_config_reads_rpc_urls_from_environment(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("database_path: monitor.db\n", encoding="utf-8")

    config = load_config(
        path,
        environ={
            "ETH_RPC_URLS": "https://eth-one.example,https://eth-two.example",
            "BSC_RPC_URLS": "https://bsc.example",
        },
    )

    assert config.chains.ethereum.rpc_urls == [
        "https://eth-one.example",
        "https://eth-two.example",
    ]
    assert config.chains.bsc.rpc_urls == ["https://bsc.example"]


def test_load_config_rejects_invalid_token_address(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "database_path: monitor.db\nchains:\n  ethereum:\n    token_address: '0x1234'\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="20-byte hex"):
        load_config(path, environ={})


def test_load_config_rejects_scan_batch_not_larger_than_overlap(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
database_path: monitor.db
chains:
  ethereum:
    chain_id: 1
    rpc_urls: [https://eth.example]
    confirmation_depth: 3
    overlap_blocks: 20
    scan_batch_blocks: 20
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="scan_batch_blocks must be larger"):
        load_config(path, environ={})


def test_chain_poll_intervals_are_independent_from_market(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
database_path: monitor.db
market:
  interval_seconds: 3600
chains:
  ethereum:
    chain_id: 1
    rpc_urls: [https://eth.example]
    confirmation_depth: 3
    interval_seconds: 30
  bsc:
    chain_id: 56
    rpc_urls: [https://bsc.example]
    confirmation_depth: 10
    interval_seconds: 15
""".strip(),
        encoding="utf-8",
    )

    config = load_config(path, environ={})

    assert config.market.interval_seconds == 3600
    assert config.chains.ethereum.interval_seconds == 30
    assert config.chains.bsc.interval_seconds == 15


def test_watched_address_requires_supported_chain(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
database_path: monitor.db
watched_addresses:
  - label: treasury
    chain: tron
    address: "0x1111111111111111111111111111111111111111"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="ethereum or bsc"):
        load_config(path, environ={})


def test_invalid_timezone_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "database_path: monitor.db\ntimezone: Mars/Olympus\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="timezone"):
        load_config(path, environ={})


def test_retention_defaults_to_180_days(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("database_path: monitor.db\n", encoding="utf-8")

    assert load_config(path, environ={}).retention_days == 180
