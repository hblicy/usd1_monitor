from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from eth_abi import decode

from usd1_monitor.collectors.reserves import selector
from usd1_monitor.models import Observation


DEFILLAMA_URL = "https://stablecoins.llama.fi/stablecoins"
EXPLORER_BASE_URLS = {
    "ethereum": "https://etherscan.io",
    "bsc": "https://bscscan.com",
}


class RpcClient(Protocol):
    async def call(self, method: str, params: list) -> object: ...


class JsonHttpClient(Protocol):
    async def get_json(
        self, url: str, params: dict[str, object] | None = None
    ) -> object: ...


class SupplyDataError(ValueError):
    pass


@dataclass(frozen=True)
class EstimatedCoverage:
    ratio_percent: float
    quality: str = "ESTIMATED"


@dataclass(frozen=True)
class SupplySnapshot:
    scope: str
    supply: float
    observed_at: datetime
    observation: Observation


def choose_defillama_asset(assets: list[dict]) -> dict:
    matches = [
        asset
        for asset in assets
        if asset.get("symbol") == "USD1"
        and asset.get("name") == "World Liberty Financial USD"
    ]
    if len(matches) != 1:
        raise SupplyDataError(
            "expected exactly one World Liberty Financial USD asset, "
            f"found {len(matches)}"
        )
    return matches[0]


def estimated_coverage(
    *, reserves: float, global_supply: float
) -> EstimatedCoverage:
    if reserves <= 0 or global_supply <= 0:
        raise SupplyDataError("reserves and global supply must be positive")
    return EstimatedCoverage(reserves / global_supply * 100)


def _decode_uint(value: object, abi_type: str, field: str) -> int:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise SupplyDataError(f"{field} response must be hex")
    try:
        return int(decode([abi_type], bytes.fromhex(value[2:]))[0])
    except Exception as exc:
        raise SupplyDataError(f"malformed {field} response") from exc


class NativeSupplyCollector:
    def __init__(self, chain: str, rpc: RpcClient, token_address: str) -> None:
        self.chain = chain
        self._rpc = rpc
        self._token_address = token_address

    async def collect(
        self, safe_block: int, collected_at: datetime
    ) -> SupplySnapshot:
        block_tag = hex(safe_block)
        raw_decimals = await self._rpc.call(
            "eth_call",
            [
                {"to": self._token_address, "data": selector("decimals()")},
                block_tag,
            ],
        )
        raw_supply = await self._rpc.call(
            "eth_call",
            [
                {"to": self._token_address, "data": selector("totalSupply()")},
                block_tag,
            ],
        )
        decimals = _decode_uint(raw_decimals, "uint8", "decimals")
        if decimals > 36:
            raise SupplyDataError("decimals must be between 0 and 36")
        raw_value = _decode_uint(raw_supply, "uint256", "totalSupply")
        supply = raw_value / (10**decimals)
        if supply <= 0:
            raise SupplyDataError("native total supply must be positive")
        raw_block = await self._rpc.call(
            "eth_getBlockByNumber", [block_tag, False]
        )
        if not isinstance(raw_block, dict):
            raise SupplyDataError("safe block response must be an object")
        raw_timestamp = raw_block.get("timestamp")
        if not isinstance(raw_timestamp, str) or not raw_timestamp.startswith("0x"):
            raise SupplyDataError("safe block timestamp must be hex")
        try:
            block_timestamp = int(raw_timestamp, 16)
            observed_at = datetime.fromtimestamp(block_timestamp, tz=UTC)
        except (ValueError, OSError, OverflowError) as exc:
            raise SupplyDataError("safe block timestamp is malformed") from exc
        if block_timestamp <= 0:
            raise SupplyDataError("safe block timestamp must be positive")
        if (observed_at - collected_at).total_seconds() > 300:
            raise SupplyDataError("safe block timestamp is in the future")
        observation = Observation(
            "supply.native",
            "evm_rpc",
            self.chain,
            float(supply),
            "USD1",
            observed_at,
            collected_at,
            quality="FACT",
            metadata={
                "block": safe_block,
                "block_timestamp": block_timestamp,
                "decimals": decimals,
                "source_url": (
                    f"{EXPLORER_BASE_URLS[self.chain]}/block/{safe_block}"
                ),
            },
        )
        return SupplySnapshot(
            self.chain, float(supply), observed_at, observation
        )


class DefiLlamaSupplyCollector:
    def __init__(
        self,
        http: JsonHttpClient,
        *,
        url: str = DEFILLAMA_URL,
        expected_symbol: str = "USD1",
        expected_name: str = "World Liberty Financial USD",
    ) -> None:
        if expected_symbol != "USD1" or expected_name != "World Liberty Financial USD":
            raise ValueError("DefiLlama USD1 identity must use the exact approved values")
        self._http = http
        self._url = url

    async def collect(self, collected_at: datetime) -> SupplySnapshot:
        body = await self._http.get_json(
            self._url, {"includePrices": "true"}
        )
        if not isinstance(body, dict) or not isinstance(
            body.get("peggedAssets"), list
        ):
            raise SupplyDataError("DefiLlama stablecoin response is malformed")
        asset = choose_defillama_asset(body["peggedAssets"])
        circulating = asset.get("circulating")
        if not isinstance(circulating, dict):
            raise SupplyDataError("DefiLlama circulating supply is missing")
        supply = circulating.get("peggedUSD")
        if not isinstance(supply, (int, float)) or supply <= 0:
            raise SupplyDataError(
                "DefiLlama circulating.peggedUSD must be positive"
            )
        observation = Observation(
            "supply.global",
            "defillama",
            "global",
            float(supply),
            "USD1",
            collected_at,
            collected_at,
            quality="ESTIMATED_SOURCE",
            metadata={
                "asset_id": asset.get("id"),
                "source_url": (
                    f"{self._url}{'&' if '?' in self._url else '?'}"
                    "includePrices=true"
                ),
            },
        )
        return SupplySnapshot(
            "global", float(supply), collected_at, observation
        )
