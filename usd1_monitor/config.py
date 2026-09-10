from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)


USD1_TOKEN_ADDRESS = "0x8d0D000Ee44948FC98c9B98A4FA4921476f08B0d"
POR_ORACLE_ADDRESS = "0x691b74146cdba162449012aa32d3cbf5df77d4c4"


class ConfigError(ValueError):
    """Raised when configuration cannot be loaded or validated."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HttpConfig(StrictModel):
    timeout_seconds: float = Field(default=10, gt=0)
    retries: int = Field(default=2, ge=0, le=5)
    max_response_bytes: int = Field(default=20_000_000, gt=0)
    rpc_throughput_cups: float = Field(default=270, gt=0)


class MarketConfig(StrictModel):
    symbols: list[str] = Field(default_factory=lambda: ["USD1USDT", "USD1USDC"])
    interval_seconds: int = Field(default=60, ge=10)
    depth_limit: int = 1000
    yellow_price: float = 0.997
    yellow_seconds: int = Field(default=900, gt=0)
    red_price: float = 0.995
    red_seconds: int = Field(default=300, gt=0)
    severe_price: float = 0.99
    severe_seconds: int = Field(default=3600, gt=0)
    recovery_price: float = 0.998
    recovery_seconds: int = Field(default=900, gt=0)
    sell_sizes: list[float] = Field(
        default_factory=lambda: [1_000_000, 5_000_000, 20_000_000]
    )

    @model_validator(mode="after")
    def validate_threshold_order(self) -> "MarketConfig":
        if self.red_price >= self.yellow_price:
            raise ValueError("red_price must be lower than yellow_price")
        if self.severe_price >= self.red_price:
            raise ValueError("severe_price must be lower than red_price")
        if self.recovery_price <= self.yellow_price:
            raise ValueError("recovery_price must be higher than yellow_price")
        if self.depth_limit != 1000:
            raise ValueError("depth_limit must be 1000 for the Binance snapshot")
        if self.symbols != ["USD1USDT", "USD1USDC"]:
            raise ValueError("symbols must be USD1USDT and USD1USDC")
        if any(size <= 0 for size in self.sell_sizes):
            raise ValueError("sell_sizes must contain positive values")
        required_sizes = {1_000_000, 5_000_000, 20_000_000}
        if not required_sizes.issubset(self.sell_sizes):
            raise ValueError(
                "sell_sizes must contain 1000000, 5000000, and 20000000"
            )
        return self


class LoggingConfig(StrictModel):
    path: Path = Path("logs/usd1-monitor.log")
    max_bytes: int = Field(default=10_000_000, gt=0)
    backup_count: int = Field(default=5, ge=0)


class ChainConfig(StrictModel):
    chain_id: int = Field(gt=0)
    token_address: str = USD1_TOKEN_ADDRESS
    rpc_urls: list[str]
    confirmation_depth: int = Field(ge=0)
    interval_seconds: int = Field(default=600, ge=5)
    overlap_blocks: int = Field(default=20, ge=1)
    scan_batch_blocks: int = Field(default=2_000, gt=0, le=10_000)
    log_query_chunk_blocks: int = Field(default=500, gt=0, le=2_000)

    @model_validator(mode="after")
    def validate_scan_progress(self) -> "ChainConfig":
        if self.scan_batch_blocks <= self.overlap_blocks:
            raise ValueError(
                "scan_batch_blocks must be larger than overlap_blocks"
            )
        if self.log_query_chunk_blocks > self.scan_batch_blocks:
            raise ValueError(
                "log_query_chunk_blocks must not exceed scan_batch_blocks"
            )
        return self

    @field_validator("token_address")
    @classmethod
    def validate_address(cls, value: str) -> str:
        if re.fullmatch(r"0x[0-9a-fA-F]{40}", value) is None:
            raise ValueError("token_address must be a 20-byte hex address")
        return value

    @field_validator("rpc_urls")
    @classmethod
    def validate_rpc_urls(cls, values: list[str]) -> list[str]:
        if not values:
            raise ValueError("rpc_urls must not be empty")
        for value in values:
            parts = urlsplit(value)
            if parts.scheme != "https" or not parts.netloc:
                raise ValueError("rpc_urls must contain valid HTTPS URLs")
        return values


class ChainsConfig(StrictModel):
    ethereum: ChainConfig = Field(
        default_factory=lambda: ChainConfig(
            chain_id=1,
            rpc_urls=[
                "https://ethereum-rpc.publicnode.com",
                "https://eth.llamarpc.com",
            ],
            confirmation_depth=3,
        )
    )
    bsc: ChainConfig = Field(
        default_factory=lambda: ChainConfig(
            chain_id=56,
            rpc_urls=[
                "https://bsc-dataseed.binance.org",
                "https://bsc-rpc.publicnode.com",
            ],
            confirmation_depth=10,
        )
    )


class WatchedAddressConfig(StrictModel):
    label: str = Field(min_length=1)
    chain: str
    address: str

    @field_validator("chain")
    @classmethod
    def validate_chain(cls, value: str) -> str:
        if value not in {"ethereum", "bsc"}:
            raise ValueError("chain must be ethereum or bsc")
        return value

    @field_validator("address")
    @classmethod
    def validate_address(cls, value: str) -> str:
        if re.fullmatch(r"0x[0-9a-fA-F]{40}", value) is None:
            raise ValueError("address must be a 20-byte hex address")
        return value


class PorConfig(StrictModel):
    address: str = POR_ORACLE_ADDRESS
    interval_seconds: int = Field(default=300, gt=0)
    yellow_staleness_seconds: int = Field(default=3600, gt=0)
    red_staleness_seconds: int = Field(default=7200, gt=0)
    relative_change_threshold: float = Field(default=0.005, gt=0)
    recovery_read_count: int = Field(default=2, ge=1)

    @field_validator("address")
    @classmethod
    def validate_address(cls, value: str) -> str:
        if re.fullmatch(r"0x[0-9a-fA-F]{40}", value) is None:
            raise ValueError("PoR address must be a 20-byte hex address")
        return value

    @model_validator(mode="after")
    def validate_staleness(self) -> "PorConfig":
        if self.red_staleness_seconds <= self.yellow_staleness_seconds:
            raise ValueError(
                "red_staleness_seconds must exceed yellow_staleness_seconds"
            )
        return self


class MultichainSupplyConfig(StrictModel):
    tron_rpc_urls: list[str] = Field(
        default_factory=lambda: [
            "https://api.trongrid.io",
            "https://api.tronstack.io",
        ]
    )
    solana_rpc_urls: list[str] = Field(
        default_factory=lambda: [
            "https://solana-rpc.publicnode.com",
            "https://api.mainnet-beta.solana.com",
        ]
    )
    aptos_indexer_urls: list[str] = Field(
        default_factory=lambda: [
            "https://api.mainnet.aptoslabs.com/v1/graphql"
        ]
    )
    tempo_rpc_urls: list[str] = Field(
        default_factory=lambda: ["https://rpc.presto.tempo.xyz"]
    )
    plume_rpc_urls: list[str] = Field(
        default_factory=lambda: ["https://rpc.plume.org"]
    )
    ab_rpc_urls: list[str] = Field(
        default_factory=lambda: ["https://rpc.core.ab.org"]
    )
    monad_rpc_urls: list[str] = Field(
        default_factory=lambda: ["https://rpc.monad.xyz"]
    )
    mantle_rpc_urls: list[str] = Field(
        default_factory=lambda: ["https://rpc.mantle.xyz"]
    )
    morph_rpc_urls: list[str] = Field(
        default_factory=lambda: ["https://rpc.morphl2.io"]
    )

    @field_validator("*", mode="after")
    @classmethod
    def validate_urls(cls, values: list[str]) -> list[str]:
        if not values:
            raise ValueError("multichain RPC URL lists must not be empty")
        for value in values:
            parts = urlsplit(value)
            if parts.scheme != "https" or not parts.netloc:
                raise ValueError(
                    "multichain RPC URLs must be valid HTTPS URLs"
                )
        return values


class SupplyConfig(StrictModel):
    interval_seconds: int = Field(default=3600, gt=0)
    defillama_url: str = "https://stablecoins.llama.fi/stablecoins"
    expected_symbol: str = "USD1"
    expected_name: str = "World Liberty Financial USD"
    multichain: MultichainSupplyConfig = Field(
        default_factory=MultichainSupplyConfig
    )

    @model_validator(mode="after")
    def validate_identity_and_url(self) -> "SupplyConfig":
        parts = urlsplit(self.defillama_url)
        if parts.scheme != "https" or not parts.netloc:
            raise ValueError("defillama_url must be a valid HTTPS URL")
        if self.expected_symbol != "USD1":
            raise ValueError("expected_symbol must be USD1")
        if self.expected_name != "World Liberty Financial USD":
            raise ValueError(
                "expected_name must be World Liberty Financial USD"
            )
        return self


class OfficialSourceConfig(StrictModel):
    url: str
    interval_seconds: int = Field(gt=0)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parts = urlsplit(value)
        try:
            port = parts.port
        except ValueError as exc:
            raise ValueError("official source URL must be valid HTTPS") from exc
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or port not in (None, 443)
        ):
            raise ValueError("official source URL must be valid HTTPS")
        return value


class InformationConfig(StrictModel):
    binance: OfficialSourceConfig = Field(
        default_factory=lambda: OfficialSourceConfig(
            url=(
                "https://www.binance.com/bapi/apex/v1/public/apex/cms/"
                "article/list/query"
            ),
            interval_seconds=900,
        )
    )
    bitgo: OfficialSourceConfig = Field(
        default_factory=lambda: OfficialSourceConfig(
            url="https://www.bitgo.com/usd1/attestations/",
            interval_seconds=21600,
        )
    )
    wlfi: OfficialSourceConfig = Field(
        default_factory=lambda: OfficialSourceConfig(
            url="https://docs.worldlibertyfinancial.com/usd1-token/proof-of-reserves",
            interval_seconds=21600,
        )
    )
    occ: OfficialSourceConfig = Field(
        default_factory=lambda: OfficialSourceConfig(
            url="https://www.occ.gov/topics/charters-and-licensing/interpretations-and-decisions/index-interpretations-and-decisions.html",
            interval_seconds=21600,
        )
    )

    @model_validator(mode="after")
    def validate_hosts(self) -> "InformationConfig":
        allowed = {
            "binance": {"www.binance.com", "binance.com"},
            "bitgo": {"www.bitgo.com", "bitgo.com", "landing.bitgo.com"},
            "wlfi": {
                "docs.worldlibertyfinancial.com",
                "por.worldlibertyfinancial.com",
                "worldlibertyfinancial.com",
            },
            "occ": {"www.occ.gov", "occ.gov"},
        }
        for source, hosts in allowed.items():
            value = getattr(self, source).url
            parts = urlsplit(value)
            if parts.scheme != "https" or parts.hostname not in hosts:
                raise ValueError(f"information.{source}.url is outside allowlist")
        return self

class AppConfig(StrictModel):
    database_path: Path
    timezone: str = "Asia/Shanghai"
    retention_days: int = Field(default=180, ge=1)
    event_active_seconds: int = Field(default=3600, ge=60)
    market: MarketConfig = Field(default_factory=MarketConfig)
    http: HttpConfig = Field(default_factory=HttpConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    chains: ChainsConfig = Field(default_factory=ChainsConfig)
    watched_addresses: list[WatchedAddressConfig] = Field(default_factory=list)
    por: PorConfig = Field(default_factory=PorConfig)
    supply: SupplyConfig = Field(default_factory=SupplyConfig)
    information: InformationConfig = Field(default_factory=InformationConfig)
    wechat_webhook: str | None = None

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        if value in {"Asia/Shanghai", "UTC"}:
            return value
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"timezone is unknown: {value}") from exc
        return value


def load_config(
    path: Path, environ: Mapping[str, str] | None = None
) -> AppConfig:
    values = os.environ if environ is None else environ
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ConfigError(f"configuration root must be a mapping at {path}")
        raw["wechat_webhook"] = values.get("WECHAT_WEBHOOK") or None
        config = AppConfig.model_validate(raw)
        ethereum_urls = _rpc_urls_from_environment(values.get("ETH_RPC_URLS"))
        bsc_urls = _rpc_urls_from_environment(values.get("BSC_RPC_URLS"))
        ethereum = config.chains.ethereum
        bsc = config.chains.bsc
        if ethereum_urls:
            ethereum = ChainConfig.model_validate(
                {**ethereum.model_dump(), "rpc_urls": ethereum_urls}
            )
        if bsc_urls:
            bsc = ChainConfig.model_validate(
                {**bsc.model_dump(), "rpc_urls": bsc_urls}
            )
        multichain_env = {
            "tron_rpc_urls": "TRON_RPC_URLS",
            "solana_rpc_urls": "SOLANA_RPC_URLS",
            "aptos_indexer_urls": "APTOS_INDEXER_URLS",
            "tempo_rpc_urls": "TEMPO_RPC_URLS",
            "plume_rpc_urls": "PLUME_RPC_URLS",
            "ab_rpc_urls": "AB_RPC_URLS",
            "monad_rpc_urls": "MONAD_RPC_URLS",
            "mantle_rpc_urls": "MANTLE_RPC_URLS",
            "morph_rpc_urls": "MORPH_RPC_URLS",
        }
        multichain_values = config.supply.multichain.model_dump()
        for field_name, environment_name in multichain_env.items():
            urls = _rpc_urls_from_environment(values.get(environment_name))
            if urls:
                multichain_values[field_name] = urls
        multichain = MultichainSupplyConfig.model_validate(multichain_values)
        supply = config.supply.model_copy(update={"multichain": multichain})
        config_dir = path.resolve().parent
        database_path = config.database_path
        log_path = config.logging.path
        if not database_path.is_absolute():
            database_path = config_dir / database_path
        if not log_path.is_absolute():
            log_path = config_dir / log_path
        return config.model_copy(
            update={
                "database_path": database_path,
                "logging": config.logging.model_copy(update={"path": log_path}),
                "chains": config.chains.model_copy(
                    update={"ethereum": ethereum, "bsc": bsc}
                ),
                "supply": supply,
            }
        )
    except ConfigError:
        raise
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ConfigError(f"invalid configuration at {path}: {exc}") from exc


def _rpc_urls_from_environment(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]
