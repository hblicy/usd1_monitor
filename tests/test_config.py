from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from usd1_monitor.config import (
    AddressEvidenceConfig,
    ChainConfig,
    ConfigError,
    CustodyAddressConfig,
    CustodyConfig,
    RedemptionConfig,
    current_date_for_timezone,
    is_verification_current,
    load_config,
)


def _evidence(url: str, kind: str = "official") -> dict[str, str]:
    return {"kind": kind, "url": url}


def test_trusted_custody_address_accepts_one_official_source() -> None:
    item = CustodyAddressConfig.model_validate(
        {
            "chain": "ethereum",
            "address": "0xf977814e90da44bfa03b6295a0616a897441acec",
            "entity": "binance_cex",
            "label": "Binance 8",
            "role": "hot_wallet",
            "status": "trusted",
            "verified_on": date.today().isoformat(),
            "evidence": [_evidence("https://www.binance.com/en/square/post/97671")],
        }
    )

    assert item.status == "trusted"


def test_trusted_custody_address_rejects_one_nonofficial_source() -> None:
    with pytest.raises(ValueError, match="official source or two independent"):
        CustodyAddressConfig.model_validate(
            {
                "chain": "bsc",
                "address": "0x8894e0a0c962cb723c1976a4421c95949be2d4e3",
                "entity": "binance_cex",
                "label": "Binance Hot Wallet",
                "role": "hot_wallet",
                "status": "trusted",
                "verified_on": date.today().isoformat(),
                "evidence": [
                    _evidence(
                        "https://bscscan.com/address/0x8894e0a0c962cb723c1976a4421c95949be2d4e3",
                        "label",
                    )
                ],
            }
        )


def test_official_evidence_must_belong_to_configured_entity() -> None:
    with pytest.raises(ValueError, match="official evidence host does not match entity"):
        CustodyAddressConfig.model_validate(
            {
                "chain": "ethereum",
                "address": "0xf977814e90da44bfa03b6295a0616a897441acec",
                "entity": "binance_cex",
                "label": "Binance 8",
                "role": "hot_wallet",
                "status": "trusted",
                "verified_on": date.today().isoformat(),
                "evidence": [_evidence("https://example.com/address-list")],
            }
        )


def test_trusted_addresses_excludes_expired_entries_at_runtime() -> None:
    config = CustodyConfig.model_validate(
        {
            "verification_max_age_days": 90,
            "addresses": [
                {
                    "chain": "solana",
                    "address": "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9",
                    "entity": "binance_cex",
                    "label": "Binance 2",
                    "role": "hot_wallet",
                    "status": "trusted",
                    "verified_on": date.today().isoformat(),
                    "evidence": [
                        _evidence("https://www.binance.com/en/wallet-addresses")
                    ],
                }
            ],
        }
    )

    assert config.trusted_addresses(as_of=date.today())
    assert not config.trusted_addresses(as_of=date.today() + timedelta(days=91))


def test_custody_rejects_invalid_address_formats() -> None:
    with pytest.raises(ValueError, match="20-byte hex"):
        CustodyAddressConfig.model_validate(
            {
                "chain": "ethereum",
                "address": "0x1234",
                "entity": "binance_cex",
                "label": "bad",
                "role": "hot_wallet",
            }
        )

    with pytest.raises(ValueError, match="32 bytes"):
        CustodyAddressConfig.model_validate(
            {
                "chain": "solana",
                "address": "z" * 44,
                "entity": "binance_cex",
                "label": "too long after base58 decoding",
                "role": "hot_wallet",
            }
        )
    with pytest.raises(ValueError, match="base58"):
        CustodyAddressConfig.model_validate(
            {
                "chain": "solana",
                "address": "0OIl",
                "entity": "binance_cex",
                "label": "bad",
                "role": "hot_wallet",
            }
        )


def test_load_config_rejects_future_verification_date(tmp_path: Path) -> None:
    tomorrow = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
    path = tmp_path / "config.yaml"
    path.write_text(
        f"""
database_path: monitor.db
timezone: Asia/Shanghai
custody:
  addresses:
    - chain: ethereum
      address: "0xf977814e90da44bfa03b6295a0616a897441acec"
      entity: binance_cex
      label: Binance 8
      role: hot_wallet
      status: trusted
      verified_on: {tomorrow}
      evidence:
        - kind: official
          url: https://www.binance.com/en/square/post/97671
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="must not be in the future"):
        load_config(path, environ={})


def test_verification_currentness_can_be_rechecked_as_time_advances() -> None:
    verified_on = date(2026, 1, 1)

    assert is_verification_current(verified_on, 90, as_of=date(2026, 3, 31))
    assert not is_verification_current(verified_on, 90, as_of=date(2026, 4, 2))
    assert not is_verification_current(
        verified_on, 90, as_of=date(2025, 12, 31)
    )


def test_timezone_date_boundary_uses_configured_timezone() -> None:
    instant = datetime(2026, 1, 1, 16, 30, tzinfo=timezone.utc)

    assert current_date_for_timezone("Asia/Shanghai", instant) == date(2026, 1, 2)


def test_custody_rejects_duplicate_addresses_and_evidence_hosts() -> None:
    address = {
        "chain": "ethereum",
        "address": "0xf977814e90da44bfa03b6295a0616a897441acec",
        "entity": "binance_cex",
        "label": "Binance 8",
        "role": "hot_wallet",
        "status": "trusted",
        "verified_on": date.today().isoformat(),
        "evidence": [
            _evidence("https://one.example/address", "label"),
            _evidence("https://www.one.example/another", "label"),
        ],
    }
    with pytest.raises(ValueError, match="allowed label evidence source"):
        CustodyAddressConfig.model_validate(address)

    address["evidence"] = [_evidence("https://www.binance.com/address")]
    with pytest.raises(ValueError, match="unique within each chain"):
        CustodyConfig.model_validate({"addresses": [address, dict(address)]})


def test_custody_keeps_solana_address_case_sensitive() -> None:
    base = {
        "chain": "solana",
        "entity": "binance_cex",
        "label": "Binance SOL wallet",
        "role": "hot_wallet",
        "status": "candidate",
    }

    config = CustodyConfig.model_validate(
        {
            "addresses": [
                {**base, "address": "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9"},
                {**base, "address": "5TzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9"},
            ]
        }
    )

    assert len(config.addresses) == 2


def test_custody_defaults_and_threshold_order() -> None:
    config = CustodyConfig()

    assert config.interval_seconds == 600
    assert config.verification_max_age_days == 90
    assert config.yellow_share == 0.50
    assert config.red_share == 0.70
    assert config.entity_flow_24h == 50_000_000
    assert config.address_outflow_1h == 100_000_000
    assert config.recovery_checks == 2

    with pytest.raises(ValueError, match="red_share must exceed yellow_share"):
        CustodyConfig(yellow_share=0.70, red_share=0.50)


def test_trusted_custody_address_accepts_two_independent_labels() -> None:
    item = CustodyAddressConfig.model_validate(
        {
            "chain": "ethereum",
            "address": "0xf977814e90da44bfa03b6295a0616a897441acec",
            "entity": "binance_cex",
            "label": "Binance 8",
            "role": "hot_wallet",
            "status": "trusted",
            "verified_on": date.today().isoformat(),
            "evidence": [
                _evidence("https://etherscan.io/address/0xf977814e90da44bfa03b6295a0616a897441acec", "label"),
                _evidence("https://bscscan.com/address/0xf977814e90da44bfa03b6295a0616a897441acec", "label"),
            ],
        }
    )

    assert item.status == "trusted"


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("https://a.etherscan.io/source", "https://b.etherscan.io/source"),
    ],
)
def test_label_evidence_rejects_subdomains_of_same_registered_source(
    first: str, second: str
) -> None:
    with pytest.raises(ValueError, match="evidence hosts must be independent"):
        CustodyAddressConfig.model_validate(
            {
                "chain": "ethereum",
                "address": "0xf977814e90da44bfa03b6295a0616a897441acec",
                "entity": "binance_cex",
                "label": "Binance 8",
                "role": "hot_wallet",
                "status": "trusted",
                "verified_on": date.today().isoformat(),
                "evidence": [_evidence(first, "label"), _evidence(second, "label")],
            }
        )


def test_label_evidence_rejects_unknown_registered_source() -> None:
    with pytest.raises(ValueError, match="allowed label evidence source"):
        CustodyAddressConfig.model_validate(
            {
                "chain": "ethereum",
                "address": "0xf977814e90da44bfa03b6295a0616a897441acec",
                "entity": "binance_cex",
                "label": "Binance 8",
                "role": "hot_wallet",
                "status": "trusted",
                "verified_on": date.today().isoformat(),
                "evidence": [
                    _evidence("https://a.provider.co.ae/source", "label"),
                    _evidence("https://b.provider.co.ae/source", "label"),
                ],
            }
        )


def test_custody_uses_custom_verification_max_age() -> None:
    config = CustodyConfig.model_validate(
        {
            "verification_max_age_days": 30,
            "addresses": [
                {
                    "chain": "ethereum",
                    "address": "0xf977814e90da44bfa03b6295a0616a897441acec",
                    "entity": "binance_cex",
                    "label": "Binance 8",
                    "role": "hot_wallet",
                    "status": "trusted",
                    "verified_on": (date.today() - timedelta(days=31)).isoformat(),
                    "evidence": [_evidence("https://www.binance.com/en/square/post/97671")],
                }
            ],
        }
    )

    assert config.trusted_addresses(as_of=date.today()) == []


def test_candidate_custody_address_accepts_one_label_without_verified_on() -> None:
    item = CustodyAddressConfig.model_validate(
        {
            "chain": "solana",
            "address": "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9",
            "entity": "binance_cex",
            "label": "Binance SOL hot wallet",
            "role": "hot_wallet",
            "status": "candidate",
            "evidence": [
                _evidence(
                    "https://solscan.io/account/5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9",
                    "label",
                )
            ],
        }
    )

    assert item.verified_on is None


def test_redemption_defaults_and_allowlists() -> None:
    config = RedemptionConfig()

    assert config.status_url == "https://status.bitgo.com/api/v2/summary.json"
    assert config.status_interval_seconds == 300
    assert config.page_interval_seconds == 3600
    assert config.recovery_checks == 2
    assert config.official_page_urls == [
        "https://www.bitgo.com/usd1/",
        "https://www.bitgo.com/usd1-terms/",
        "https://investors.bitgo.com/news/default.aspx",
        "https://docs.worldlibertyfinancial.com/resources/faq",
    ]
    assert config.media_rss_urls == []

    with pytest.raises(ValueError, match="status.bitgo.com"):
        RedemptionConfig(status_url="https://status.example.com/summary.json")
    with pytest.raises(ValueError, match="outside allowlist"):
        RedemptionConfig(official_page_urls=["https://example.com/news"])
    with pytest.raises(ValueError, match="media RSS URLs must be HTTPS"):
        RedemptionConfig(media_rss_urls=["http://example.com/feed.xml"])


def test_address_evidence_requires_https() -> None:
    with pytest.raises(ValueError, match="valid HTTPS"):
        AddressEvidenceConfig(kind="label", url="http://example.com")


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
    assert config.http.rpc_throughput_cups == 270
    assert config.wechat_webhook is None


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


def test_load_config_rejects_non_positive_rpc_throughput(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "database_path: monitor.db\nhttp:\n  rpc_throughput_cups: 0\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="greater than 0"):
        load_config(path, environ={})


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


def test_old_config_gets_multichain_public_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("database_path: monitor.db\n", encoding="utf-8")

    config = load_config(path, environ={})

    assert config.supply.multichain.tempo_rpc_urls == [
        "https://rpc.presto.tempo.xyz"
    ]
    assert config.supply.multichain.aptos_indexer_urls == [
        "https://api.mainnet.aptoslabs.com/v1/graphql"
    ]


def test_multichain_rpc_environment_overrides_are_comma_separated(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("database_path: monitor.db\n", encoding="utf-8")

    config = load_config(
        path,
        environ={
            "TEMPO_RPC_URLS": (
                "https://tempo-one.example,https://tempo-two.example"
            ),
            "SOLANA_RPC_URLS": "https://solana.example",
        },
    )

    assert config.supply.multichain.tempo_rpc_urls == [
        "https://tempo-one.example",
        "https://tempo-two.example",
    ]
    assert config.supply.multichain.solana_rpc_urls == [
        "https://solana.example"
    ]


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


def test_chain_config_accepts_permission_monitor_scan_sizes() -> None:
    chain = ChainConfig(
        chain_id=56,
        rpc_urls=["https://bsc.example.com"],
        confirmation_depth=10,
        interval_seconds=600,
        overlap_blocks=20,
        scan_batch_blocks=2_000,
        log_query_chunk_blocks=500,
    )

    assert chain.scan_batch_blocks == 2_000
    assert chain.log_query_chunk_blocks == 500


def test_chain_config_rejects_log_chunk_larger_than_scan_batch() -> None:
    with pytest.raises(ValueError, match="log_query_chunk_blocks"):
        ChainConfig(
            chain_id=1,
            rpc_urls=["https://eth.example.com"],
            confirmation_depth=3,
            overlap_blocks=20,
            scan_batch_blocks=100,
            log_query_chunk_blocks=101,
        )


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
