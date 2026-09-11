from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, Sequence

from usd1_monitor.config import CustodyAddressConfig, USD1_TOKEN_ADDRESS
from usd1_monitor.models import Observation
from usd1_monitor.storage import Storage


BALANCE_OF_SELECTOR = "0x70a08231"
SOLANA_USD1_MINT = "USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB"
_UINT256_HEX = re.compile(r"0x[0-9a-fA-F]{64}")
_HEX_QUANTITY = re.compile(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)")
_DECIMAL_UINT = re.compile(r"[0-9]+")


class RpcClient(Protocol):
    async def call(self, method: str, params: list) -> object: ...


class CustodyDataError(ValueError):
    pass


@dataclass(frozen=True)
class CustodyFailure:
    chain: str
    address: str
    label: str
    error: Exception


@dataclass(frozen=True)
class CustodyCollection:
    observations: tuple[Observation, ...]
    errors: tuple[CustodyFailure, ...] = ()
    trusted_complete: bool = True
    safe_block: int | str | None = None

    @property
    def trusted_balance(self) -> float | None:
        if not self.trusted_complete:
            return None
        return sum(
            item.value
            for item in self.observations
            if item.metadata.get("status") == "trusted"
        )


def balance_of_data(address: str) -> str:
    if re.fullmatch(r"0x[0-9a-fA-F]{40}", address) is None:
        raise CustodyDataError("invalid EVM custody address")
    return BALANCE_OF_SELECTOR + address[2:].lower().rjust(64, "0")


def _address_key(item: CustodyAddressConfig) -> tuple[str, str]:
    address = (
        item.address.casefold()
        if item.chain in {"ethereum", "bsc"}
        else item.address
    )
    return item.chain, address


def _unique_addresses(
    addresses: Sequence[CustodyAddressConfig],
) -> list[CustodyAddressConfig]:
    unique: list[CustodyAddressConfig] = []
    seen: set[tuple[str, str]] = set()
    for item in addresses:
        key = _address_key(item)
        if key not in seen:
            unique.append(item)
            seen.add(key)
    return unique


def _effective_keys(
    addresses: Sequence[CustodyAddressConfig],
    trusted_addresses: Sequence[CustodyAddressConfig],
) -> set[tuple[str, str]]:
    available = {_address_key(item) for item in addresses}
    effective = {_address_key(item) for item in trusted_addresses}
    if not effective.issubset(available):
        raise CustodyDataError(
            "effective trusted addresses must be part of the collected address set"
        )
    return effective


def _metadata(
    item: CustodyAddressConfig,
    *,
    effective_trusted: bool,
    safe_block: int | str,
    max_age_seconds: int,
) -> dict[str, object]:
    return {
        "entity": item.entity,
        "label": item.label,
        "role": item.role,
        "status": "trusted" if effective_trusted else "candidate",
        "configured_status": item.status,
        "evidence_urls": [entry.url for entry in item.evidence],
        "verified_on": (
            item.verified_on.isoformat() if item.verified_on is not None else None
        ),
        "safe_block": safe_block,
        "max_age_seconds": max_age_seconds,
    }


def _parse_block_number(value: object, chain: str) -> int:
    if not isinstance(value, str) or _HEX_QUANTITY.fullmatch(value) is None:
        raise CustodyDataError(f"{chain} eth_blockNumber result is malformed")
    return int(value, 16)


def _parse_evm_balance(value: object, chain: str, address: str) -> int:
    if not isinstance(value, str) or _UINT256_HEX.fullmatch(value) is None:
        raise CustodyDataError(
            f"{chain} custody address {address} balanceOf result is malformed"
        )
    return int(value, 16)


async def enrich_transfer_timestamps(
    chain: str,
    rpc: RpcClient,
    storage: Storage,
    addresses: set[str],
    *,
    min_block: int,
) -> None:
    blocks = await storage.unstamped_transfer_blocks(
        chain, addresses, min_block=min_block
    )
    method = "eth_getBlockByNumber"
    for block_number in blocks:
        try:
            body = await rpc.call(method, [hex(block_number), False])
        except Exception as exc:
            raise CustodyDataError(
                f"{chain} block {block_number} {method} failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(body, Mapping):
            raise CustodyDataError(
                f"{chain} block {block_number} {method} response is malformed"
            )
        raw_number = body.get("number")
        if (
            not isinstance(raw_number, str)
            or _HEX_QUANTITY.fullmatch(raw_number) is None
            or int(raw_number, 16) != block_number
        ):
            raise CustodyDataError(
                f"{chain} block {block_number} {method} number is malformed "
                "or mismatched"
            )
        raw_timestamp = body.get("timestamp")
        if (
            not isinstance(raw_timestamp, str)
            or _HEX_QUANTITY.fullmatch(raw_timestamp) is None
        ):
            raise CustodyDataError(
                f"{chain} block {block_number} {method} timestamp is malformed"
            )
        try:
            block_time = datetime.fromtimestamp(int(raw_timestamp, 16), tz=UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise CustodyDataError(
                f"{chain} block {block_number} {method} timestamp is malformed: "
                f"{exc}"
            ) from exc
        try:
            await storage.set_transfer_block_time(
                chain, block_number, block_time
            )
        except Exception as exc:
            raise CustodyDataError(
                f"{chain} block {block_number} {method} timestamp persistence "
                f"failed: {type(exc).__name__}: {exc}"
            ) from exc


def _parse_solana_accounts(value: object, owner: str) -> tuple[int, int, int]:
    if not isinstance(value, dict):
        raise CustodyDataError("Solana token accounts response is malformed")
    context = value.get("context")
    if not isinstance(context, dict):
        raise CustodyDataError("Solana response context.slot is malformed")
    slot = context.get("slot")
    if isinstance(slot, bool) or not isinstance(slot, int) or slot < 0:
        raise CustodyDataError("Solana response context.slot is malformed")
    if not isinstance(value.get("value"), list):
        raise CustodyDataError("Solana token accounts response is malformed")

    raw_total = 0
    seen_accounts: set[str] = set()
    accounts = value["value"]
    for row in accounts:
        if not isinstance(row, dict):
            raise CustodyDataError("Solana token account row is malformed")
        pubkey = row.get("pubkey")
        if not isinstance(pubkey, str) or not pubkey:
            raise CustodyDataError("Solana token account pubkey is malformed")
        if pubkey in seen_accounts:
            raise CustodyDataError(f"duplicate Solana token account {pubkey}")
        seen_accounts.add(pubkey)

        account = row.get("account")
        if not isinstance(account, dict):
            raise CustodyDataError("Solana token account is malformed")
        data = account.get("data")
        if not isinstance(data, dict):
            raise CustodyDataError("Solana token account data is malformed")
        parsed = data.get("parsed")
        if not isinstance(parsed, dict):
            raise CustodyDataError("Solana parsed token account is malformed")
        info = parsed.get("info")
        if not isinstance(info, dict):
            raise CustodyDataError("Solana token account info is malformed")
        token_amount = info.get("tokenAmount")
        if not isinstance(token_amount, dict):
            raise CustodyDataError("Solana tokenAmount is malformed")
        if info.get("mint") != SOLANA_USD1_MINT:
            raise CustodyDataError("Solana token account mint identity mismatch")
        if info.get("owner") != owner:
            raise CustodyDataError("Solana token account owner identity mismatch")
        decimals = token_amount.get("decimals")
        if isinstance(decimals, bool) or decimals != 6:
            raise CustodyDataError("Solana token account decimals must be 6")
        amount = token_amount.get("amount")
        if not isinstance(amount, str) or _DECIMAL_UINT.fullmatch(amount) is None:
            raise CustodyDataError("Solana token account amount is malformed")
        raw_amount = int(amount)
        if raw_amount > 2**64 - 1:
            raise CustodyDataError("Solana token account amount exceeds uint64")
        raw_total += raw_amount
    return slot, raw_total, len(accounts)


class CustodyBalanceCollector:
    """Collect all display balances using a caller-provided effective trust set.

    Callers must derive ``trusted_addresses`` from
    ``CustodyConfig.trusted_addresses(as_of)`` for every collection cycle. The
    separate ``addresses`` argument may include candidates and expired trusted
    entries that should remain visible without entering trusted aggregates.
    """

    def __init__(
        self,
        evm_rpcs: dict[str, RpcClient],
        solana_rpc: RpcClient | None,
        *,
        interval_seconds: int = 600,
        token_address: str = USD1_TOKEN_ADDRESS,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if re.fullmatch(r"0x[0-9a-fA-F]{40}", token_address) is None:
            raise ValueError("token_address must be a 20-byte hex address")
        self._evm_rpcs = dict(evm_rpcs)
        self._solana_rpc = solana_rpc
        self._interval_seconds = interval_seconds
        self._token_address = token_address

    async def collect_evm(
        self,
        chain: str,
        addresses: Sequence[CustodyAddressConfig],
        confirmation_depth: int,
        collected_at: datetime,
        *,
        trusted_addresses: Sequence[CustodyAddressConfig],
    ) -> CustodyCollection:
        if isinstance(confirmation_depth, bool) or confirmation_depth < 0:
            raise CustodyDataError("confirmation_depth must be non-negative")
        unique = _unique_addresses(addresses)
        if any(item.chain != chain for item in unique):
            raise CustodyDataError(
                f"all custody addresses must belong to requested chain {chain}"
            )
        trusted_keys = _effective_keys(unique, trusted_addresses)
        rpc = self._evm_rpcs.get(chain)
        if rpc is None:
            raise CustodyDataError(f"no custody RPC configured for {chain}")

        try:
            head = _parse_block_number(
                await rpc.call("eth_blockNumber", []), chain
            )
        except CustodyDataError:
            raise
        except Exception as exc:
            raise CustodyDataError(
                f"{chain} eth_blockNumber failed: {type(exc).__name__}: {exc}"
            ) from exc
        safe_block = head - confirmation_depth
        if safe_block < 0:
            raise CustodyDataError(f"{chain} has no confirmed custody block")

        observations: list[Observation] = []
        errors: list[CustodyFailure] = []
        trusted_complete = True
        for item in unique:
            key = _address_key(item)
            is_trusted = key in trusted_keys
            try:
                raw = await rpc.call(
                    "eth_call",
                    [
                        {
                            "to": self._token_address,
                            "data": balance_of_data(item.address),
                        },
                        hex(safe_block),
                    ],
                )
                amount = _parse_evm_balance(raw, chain, item.address)
                balance = amount / 10**18
                if not math.isfinite(balance):
                    raise CustodyDataError(
                        f"{chain} custody address {item.address} balance is not finite"
                    )
                metadata = _metadata(
                    item,
                    effective_trusted=is_trusted,
                    safe_block=safe_block,
                    max_age_seconds=self._interval_seconds * 2,
                )
                metadata["raw_amount"] = str(amount)
                observations.append(
                    Observation(
                        "custody.address_balance",
                        "evm_rpc",
                        f"{chain}:{item.address.casefold()}",
                        float(balance),
                        "USD1",
                        collected_at,
                        collected_at,
                        quality="FACT",
                        metadata=metadata,
                    )
                )
            except Exception as exc:
                error = CustodyDataError(
                    f"{chain} custody address {item.address} ({item.label}) failed: "
                    f"{type(exc).__name__}: {exc}"
                )
                errors.append(
                    CustodyFailure(chain, item.address, item.label, error)
                )
                if is_trusted:
                    trusted_complete = False
        return CustodyCollection(
            tuple(observations),
            tuple(errors),
            trusted_complete,
            safe_block,
        )

    async def collect_solana(
        self,
        addresses: Sequence[CustodyAddressConfig],
        collected_at: datetime,
        *,
        trusted_addresses: Sequence[CustodyAddressConfig],
    ) -> CustodyCollection:
        unique = _unique_addresses(addresses)
        if any(item.chain != "solana" for item in unique):
            raise CustodyDataError(
                "all custody addresses must belong to requested chain solana"
            )
        trusted_keys = _effective_keys(unique, trusted_addresses)
        if self._solana_rpc is None:
            raise CustodyDataError("no custody RPC configured for solana")

        async def fetch(
            item: CustodyAddressConfig,
        ) -> tuple[int, int, int] | Exception:
            try:
                body = await self._solana_rpc.call(
                    "getTokenAccountsByOwner",
                    [
                        item.address,
                        {"mint": SOLANA_USD1_MINT},
                        {
                            "encoding": "jsonParsed",
                            "commitment": "finalized",
                        },
                    ],
                )
                return _parse_solana_accounts(body, item.address)
            except Exception as exc:
                return exc

        fetched = await asyncio.gather(*(fetch(item) for item in unique))
        parsed = [
            (item, result)
            for item, result in zip(unique, fetched, strict=True)
            if not isinstance(result, Exception)
        ]
        trusted_slots = [
            result[0]
            for item, result in parsed
            if _address_key(item) in trusted_keys
        ]
        reference_slot = (
            trusted_slots[0]
            if trusted_slots
            else (parsed[0][1][0] if parsed else None)
        )

        observations: list[Observation] = []
        errors: list[CustodyFailure] = []
        trusted_complete = True
        for item, result in zip(unique, fetched, strict=True):
            key = _address_key(item)
            is_trusted = key in trusted_keys
            if isinstance(result, Exception):
                error = CustodyDataError(
                    f"solana custody address {item.address} ({item.label}) failed: "
                    f"{type(result).__name__}: {result}"
                )
                errors.append(
                    CustodyFailure("solana", item.address, item.label, error)
                )
                if is_trusted:
                    trusted_complete = False
                continue

            slot, raw_total, token_accounts = result
            if (
                is_trusted
                and reference_slot is not None
                and slot != reference_slot
            ):
                error = CustodyDataError(
                    f"solana custody address {item.address} ({item.label}) "
                    f"slot drift: expected {reference_slot}, received {slot}"
                )
                errors.append(
                    CustodyFailure("solana", item.address, item.label, error)
                )
                if is_trusted:
                    trusted_complete = False
                continue
            try:
                metadata = _metadata(
                    item,
                    effective_trusted=is_trusted,
                    safe_block=slot,
                    max_age_seconds=self._interval_seconds * 2,
                )
                metadata.update(
                    {
                        "commitment": "finalized",
                        "token_accounts": token_accounts,
                        "raw_amount": str(raw_total),
                    }
                )
                observations.append(
                    Observation(
                        "custody.address_balance",
                        "solana_rpc",
                        f"solana:{item.address}",
                        float(raw_total / 10**6),
                        "USD1",
                        collected_at,
                        collected_at,
                        quality="FACT",
                        metadata=metadata,
                    )
                )
            except Exception as exc:
                error = CustodyDataError(
                    f"solana custody address {item.address} ({item.label}) failed: "
                    f"{type(exc).__name__}: {exc}"
                )
                errors.append(
                    CustodyFailure("solana", item.address, item.label, error)
                )
                if is_trusted:
                    trusted_complete = False
        return CustodyCollection(
            tuple(observations),
            tuple(errors),
            trusted_complete,
            reference_slot,
        )
