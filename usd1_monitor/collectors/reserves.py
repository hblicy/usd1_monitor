from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from eth_abi import decode
from eth_utils import keccak

from usd1_monitor.models import Observation


class RpcClient(Protocol):
    async def call(self, method: str, params: list) -> object: ...


class PorDataError(ValueError):
    pass


@dataclass(frozen=True)
class PorSnapshot:
    reserves: float
    oracle_timestamp: int
    observed_at: datetime
    collected_at: datetime
    observations: tuple[Observation, ...] = ()


def selector(signature: str) -> str:
    return "0x" + keccak(text=signature)[:4].hex()


def _rpc_data(value: object) -> bytes:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise PorDataError("malformed RPC hex data")
    try:
        return bytes.fromhex(value[2:])
    except ValueError as exc:
        raise PorDataError("malformed RPC hex data") from exc


def decode_por_calls(
    *,
    latest_bundle: str,
    latest_timestamp: str,
    bundle_decimals: str,
    collected_at: datetime,
) -> PorSnapshot:
    if collected_at.tzinfo is None:
        raise PorDataError("collected_at must be timezone-aware")
    try:
        bundle = decode(["bytes"], _rpc_data(latest_bundle))[0]
        bundle_timestamp, raw_reserves = decode(
            ["uint256", "uint256"], bundle
        )
        timestamp = decode(["uint256"], _rpc_data(latest_timestamp))[0]
        decimals = decode(["uint8[]"], _rpc_data(bundle_decimals))[0]
    except PorDataError:
        raise
    except Exception as exc:
        raise PorDataError("malformed PoR ABI response") from exc

    if bundle_timestamp != timestamp:
        raise PorDataError(
            "timestamp mismatch between latestBundle and latestBundleTimestamp"
        )
    if timestamp <= 0:
        raise PorDataError("oracle timestamp must be positive")
    if timestamp > int(collected_at.timestamp()) + 300:
        raise PorDataError("oracle timestamp is more than five minutes in the future")
    if len(decimals) < 1:
        raise PorDataError("bundleDecimals must contain the reserves entry")
    reserve_decimals = int(decimals[0])
    if reserve_decimals > 36:
        raise PorDataError("reserves decimals must be at most 36")
    reserves = raw_reserves / (10**reserve_decimals)
    if reserves <= 0:
        raise PorDataError("oracle reserves must be positive")
    observed_at = datetime.fromtimestamp(timestamp, tz=collected_at.tzinfo)
    return PorSnapshot(float(reserves), int(timestamp), observed_at, collected_at)


class PorCollector:
    def __init__(self, rpc: RpcClient, oracle_address: str) -> None:
        self._rpc = rpc
        self._oracle_address = oracle_address

    async def collect(
        self, safe_block: int, collected_at: datetime
    ) -> PorSnapshot:
        block_tag = hex(safe_block)
        values = []
        for signature in (
            "latestBundle()",
            "latestBundleTimestamp()",
            "bundleDecimals()",
        ):
            values.append(
                await self._rpc.call(
                    "eth_call",
                    [
                        {
                            "to": self._oracle_address,
                            "data": selector(signature),
                        },
                        block_tag,
                    ],
                )
            )
        snapshot = decode_por_calls(
            latest_bundle=values[0],
            latest_timestamp=values[1],
            bundle_decimals=values[2],
            collected_at=collected_at,
        )
        age = collected_at.timestamp() - snapshot.oracle_timestamp
        source_urls = [
            f"https://etherscan.io/address/{self._oracle_address}#readContract",
            f"https://etherscan.io/block/{safe_block}",
        ]
        observations = (
            Observation(
                "por.reserves",
                "por_oracle",
                "ethereum",
                snapshot.reserves,
                "USD",
                snapshot.observed_at,
                collected_at,
                metadata={"block": safe_block, "source_urls": source_urls},
            ),
            Observation(
                "por.oracle_age_seconds",
                "por_oracle",
                "ethereum",
                age,
                "seconds",
                snapshot.observed_at,
                collected_at,
                metadata={"block": safe_block, "source_urls": source_urls},
            ),
        )
        return PorSnapshot(
            snapshot.reserves,
            snapshot.oracle_timestamp,
            snapshot.observed_at,
            collected_at,
            observations,
        )
