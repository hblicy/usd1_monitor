from pathlib import Path

from usd1_monitor.config import load_config


def test_example_config_is_loadable() -> None:
    config = load_config(Path("config.example.yaml"), environ={})

    assert set(config.chains.model_dump()) == {"ethereum", "bsc"}
    assert config.por.interval_seconds == 300
    assert config.information.binance.interval_seconds == 900
