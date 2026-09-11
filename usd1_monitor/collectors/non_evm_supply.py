from __future__ import annotations

from datetime import datetime
from typing import Protocol

from usd1_monitor.collectors.supply import (
    ComponentBatch,
    SupplyDataError,
    SupplySnapshot,
)
from usd1_monitor.http import sanitize_url
from usd1_monitor.models import Observation
from usd1_monitor.supply_assets import (
    APTOS_METADATA,
    APTOS_POOL,
    SOLANA_MINT,
    SOLANA_POOL_TOKEN_ACCOUNT,
    TRON_TOKEN,
)


APTOS_EXPLORER = f"https://explorer.aptoslabs.com/object/{APTOS_METADATA}"
APTOS_QUERY = """
query Usd1Supply($asset: String!, $owner: String!) {
  fungible_asset_metadata(where: {asset_type: {_eq: $asset}}, limit: 2) {
    asset_type
    decimals
    supply_v2
  }
  current_fungible_asset_balances(
    where: {asset_type: {_eq: $asset}, owner_address: {_eq: $owner}},
    limit: 2
  ) {
    asset_type
    owner_address
    amount
  }
}
"""


class JsonPoster(Protocol):
    async def post_json(self, url: str, payload: dict) -> object: ...


class RpcClient(Protocol):
    async def call(self, method: str, params: list) -> object: ...


def _parse_uint(value: object, error: str) -> int:
    if isinstance(value, bool):
        raise SupplyDataError(error)
    if isinstance(value, int):
        if value < 0:
            raise SupplyDataError(error)
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise SupplyDataError(error)


async def _post_with_fallback(
    http: JsonPoster,
    urls: tuple[str, ...],
    path: str,
    payload: dict,
) -> object:
    failures: list[str] = []
    for base_url in urls:
        url = (
            f"{base_url.rstrip('/')}/{path.lstrip('/')}"
            if path
            else base_url
        )
        try:
            return await http.post_json(url, payload)
        except Exception as exc:
            failures.append(f"{sanitize_url(url)}: {type(exc).__name__}")
    raise SupplyDataError(
        "all non-EVM endpoints failed: " + " | ".join(failures)
    )


def _snapshot(
    component_id: str,
    metric: str,
    source: str,
    scope: str,
    raw_amount: int,
    decimals: int,
    collected_at: datetime,
    source_url: str,
) -> SupplySnapshot:
    if raw_amount < 0 or (metric == "supply.native" and raw_amount == 0):
        raise SupplyDataError(f"{component_id} amount is invalid")
    value = raw_amount / (10**decimals)
    observation = Observation(
        metric,
        source,
        scope,
        float(value),
        "USD1",
        collected_at,
        collected_at,
        quality="FACT",
        metadata={
            "component_id": component_id,
            "decimals": decimals,
            "source_url": source_url,
        },
    )
    return SupplySnapshot(scope, float(value), collected_at, observation)


class TronSupplyCollector:
    component_ids = frozenset({"native_tron"})

    def __init__(self, http: JsonPoster, urls: list[str]) -> None:
        self._http = http
        self._urls = tuple(urls)

    async def collect(self, collected_at: datetime) -> ComponentBatch:
        try:
            body = await _post_with_fallback(
                self._http,
                self._urls,
                "wallet/triggerconstantcontract",
                {
                    "owner_address": TRON_TOKEN,
                    "contract_address": TRON_TOKEN,
                    "function_selector": "totalSupply()",
                    "parameter": "",
                    "visible": True,
                },
            )
            if (
                not isinstance(body, dict)
                or not isinstance(body.get("result"), dict)
                or body["result"].get("result") is not True
            ):
                raise SupplyDataError("native_tron call was rejected")
            results = body.get("constant_result")
            if (
                not isinstance(results, list)
                or len(results) != 1
                or not isinstance(results[0], str)
            ):
                raise SupplyDataError(
                    "native_tron constant_result is malformed"
                )
            raw = int(results[0], 16)
            snapshot = _snapshot(
                "native_tron",
                "supply.native",
                "tron",
                "tron",
                raw,
                18,
                collected_at,
                f"https://tronscan.org/#/token20/{TRON_TOKEN}",
            )
            return ComponentBatch((snapshot,))
        except Exception as exc:
            return ComponentBatch((), (("native_tron", exc),))


class SolanaSupplyCollector:
    component_ids = frozenset({"native_solana", "locked_solana"})

    def __init__(self, rpc: RpcClient) -> None:
        self._rpc = rpc

    async def collect(self, collected_at: datetime) -> ComponentBatch:
        specs = (
            ("native_solana", "supply.native", SOLANA_MINT, "supply"),
            (
                "locked_solana",
                "bridge.locked",
                SOLANA_POOL_TOKEN_ACCOUNT,
                "tokenAmount",
            ),
        )
        snapshots: list[SupplySnapshot] = []
        errors: list[tuple[str, Exception]] = []
        for component_id, metric, address, field in specs:
            try:
                body = await self._rpc.call(
                    "getAccountInfo",
                    [
                        address,
                        {
                            "encoding": "jsonParsed",
                            "commitment": "finalized",
                        },
                    ],
                )
                info = body["value"]["data"]["parsed"]["info"]
                amount = (
                    info[field]
                    if field == "supply"
                    else info[field]["amount"]
                )
                decimals = (
                    info["decimals"]
                    if field == "supply"
                    else info[field]["decimals"]
                )
                if (
                    decimals != 6
                    or not isinstance(amount, str)
                    or not amount.isdigit()
                ):
                    raise SupplyDataError(
                        f"{component_id} parsed amount is malformed"
                    )
                if (
                    field == "tokenAmount"
                    and info.get("mint") != SOLANA_MINT
                ):
                    raise SupplyDataError(
                        "locked_solana mint identity mismatch"
                    )
                snapshots.append(
                    _snapshot(
                        component_id,
                        metric,
                        "solana_rpc",
                        "solana",
                        int(amount),
                        6,
                        collected_at,
                        f"https://solscan.io/account/{address}",
                    )
                )
            except Exception as exc:
                errors.append((component_id, exc))
        return ComponentBatch(tuple(snapshots), tuple(errors))


class AptosSupplyCollector:
    component_ids = frozenset({"native_aptos", "locked_aptos"})

    def __init__(self, http: JsonPoster, urls: list[str]) -> None:
        self._http = http
        self._urls = tuple(urls)

    async def collect(self, collected_at: datetime) -> ComponentBatch:
        try:
            body = await _post_with_fallback(
                self._http,
                self._urls,
                "",
                {
                    "query": APTOS_QUERY,
                    "variables": {
                        "asset": APTOS_METADATA,
                        "owner": APTOS_POOL,
                    },
                },
            )
        except Exception as exc:
            return ComponentBatch(
                (),
                (("native_aptos", exc), ("locked_aptos", exc)),
            )

        if not isinstance(body, dict) or body.get("errors"):
            exc = SupplyDataError(
                "Aptos indexer response contains errors"
            )
            return ComponentBatch(
                (),
                (("native_aptos", exc), ("locked_aptos", exc)),
            )
        data = body.get("data")
        if not isinstance(data, dict):
            exc = SupplyDataError("Aptos indexer data is malformed")
            return ComponentBatch(
                (),
                (("native_aptos", exc), ("locked_aptos", exc)),
            )

        snapshots: list[SupplySnapshot] = []
        errors: list[tuple[str, Exception]] = []
        metadata = data.get("fungible_asset_metadata")
        try:
            if not isinstance(metadata, list) or len(metadata) != 1:
                raise SupplyDataError(
                    "Aptos USD1 metadata row must be unique"
                )
            row = metadata[0]
            if (
                not isinstance(row, dict)
                or row.get("asset_type") != APTOS_METADATA
                or row.get("decimals") != 6
            ):
                raise SupplyDataError(
                    "Aptos USD1 metadata identity mismatch"
                )
            raw_supply = _parse_uint(
                row.get("supply_v2"),
                "Aptos USD1 supply_v2 is malformed",
            )
            snapshots.append(
                _snapshot(
                    "native_aptos",
                    "supply.native",
                    "aptos_indexer",
                    "aptos",
                    raw_supply,
                    6,
                    collected_at,
                    APTOS_EXPLORER,
                )
            )
        except Exception as exc:
            errors.append(("native_aptos", exc))

        balances = data.get("current_fungible_asset_balances")
        try:
            if not isinstance(balances, list):
                raise SupplyDataError(
                    "Aptos USD1 pool row must be unique"
                )
            if not balances:
                raw_balance = 0
            elif len(balances) != 1:
                raise SupplyDataError(
                    "Aptos USD1 pool row must be unique"
                )
            else:
                row = balances[0]
                if (
                    not isinstance(row, dict)
                    or row.get("asset_type") != APTOS_METADATA
                    or row.get("owner_address") != APTOS_POOL
                ):
                    raise SupplyDataError(
                        "Aptos USD1 pool identity mismatch"
                    )
                raw_balance = _parse_uint(
                    row.get("amount"),
                    "Aptos USD1 pool amount is malformed",
                )
            snapshots.append(
                _snapshot(
                    "locked_aptos",
                    "bridge.locked",
                    "aptos_indexer",
                    "aptos",
                    raw_balance,
                    6,
                    collected_at,
                    APTOS_EXPLORER,
                )
            )
        except Exception as exc:
            errors.append(("locked_aptos", exc))
        return ComponentBatch(tuple(snapshots), tuple(errors))
