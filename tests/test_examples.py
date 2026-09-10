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
