from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from eth_utils import keccak

from usd1_monitor.config import USD1_TOKEN_ADDRESS
from usd1_monitor.evm_abi import DecodedEvent, ZERO_ADDRESS, decode_log
from usd1_monitor.models import ChainEvent
from usd1_monitor.rpc import RpcResponseError
from usd1_monitor.storage import Storage


IMPLEMENTATION_SLOT = hex(
    int.from_bytes(keccak(text="eip1967.proxy.implementation"), "big") - 1
)
ADMIN_SLOT = hex(int.from_bytes(keccak(text="eip1967.proxy.admin"), "big") - 1)
OWNER_SELECTOR = "0x8da5cb5b"
PAUSED_SELECTOR = "0x5c975abb"
FROZEN_SELECTOR = "0xd0516650"


class RpcClient(Protocol):
    async def call(self, method: str, params: list) -> object: ...


class EvmScanError(ValueError):
    pass


@dataclass(frozen=True)
class ScanResult:
    latest: int
    safe_head: int
    cursor: int | None
    event_count: int
    start_block: int | None = None
    new_events: tuple[ChainEvent, ...] = ()


@dataclass(frozen=True)
class EvmSnapshot:
    chain: str
    block_number: int
    implementation: str
    admin: str
    code_hash: str
    owner: str | None
    paused: bool | None
    observations: tuple[object, ...]
    frozen_accounts: dict[str, bool] = field(default_factory=dict)


def scan_range(
    latest: int,
    confirmation_depth: int,
    cursor: int | None,
    overlap: int,
) -> tuple[int, int] | None:
    safe_head = latest - confirmation_depth
    if safe_head < 0:
        return None
    start = max(
        0,
        (cursor + 1 - overlap) if cursor is not None else safe_head - overlap + 1,
    )
    return None if start > safe_head else (start, safe_head)


class EvmScanner:
    def __init__(
        self,
        chain: str,
        rpc: RpcClient,
        storage: Storage,
        *,
        confirmation_depth: int,
        overlap_blocks: int,
        batch_blocks: int,
        token_address: str = USD1_TOKEN_ADDRESS,
    ) -> None:
        if confirmation_depth < 0 or overlap_blocks < 1 or batch_blocks < 1:
            raise ValueError("invalid EVM scanner range configuration")
        if batch_blocks <= overlap_blocks:
            raise ValueError("batch_blocks must be larger than overlap_blocks")
        self.chain = chain
        self._rpc = rpc
        self._storage = storage
        self._confirmation_depth = confirmation_depth
        self._overlap_blocks = overlap_blocks
        self._batch_blocks = batch_blocks
        self._token_address = token_address

    async def scan_once(self) -> ScanResult:
        raw_latest = await self._rpc.call("eth_blockNumber", [])
        latest = self._hex_int(raw_latest, "latest block")
        safe_head = latest - self._confirmation_depth
        cursor = await self._storage.get_scan_cursor(self.chain)
        if cursor is not None and safe_head < cursor:
            raise EvmScanError(
                f"safe head {safe_head} is behind persisted cursor {cursor}"
            )
        current_range = scan_range(
            latest, self._confirmation_depth, cursor, self._overlap_blocks
        )
        if current_range is None:
            return ScanResult(latest, safe_head, cursor, 0)

        start, end = current_range
        end = min(end, start + self._batch_blocks - 1)
        candidate_events: list[ChainEvent] = []
        for batch_start in range(start, end + 1, self._batch_blocks):
            batch_end = min(end, batch_start + self._batch_blocks - 1)
            raw_logs = await self._rpc.call(
                "eth_getLogs",
                [
                    {
                        "address": self._token_address,
                        "fromBlock": hex(batch_start),
                        "toBlock": hex(batch_end),
                    }
                ],
            )
            if not isinstance(raw_logs, list):
                raise EvmScanError("eth_getLogs result must be a list")
            observed_at = datetime.now(UTC)
            events = [self._normalize_log(item, observed_at) for item in raw_logs]
            candidate_events.extend(events)
            cursor = batch_end
        return ScanResult(
            latest,
            safe_head,
            cursor,
            len(candidate_events),
            start_block=start,
            new_events=tuple(candidate_events),
        )

    def _normalize_log(
        self, raw: object, observed_at: datetime
    ) -> ChainEvent:
        decoded = decode_log(self.chain, raw, decimals=18)
        payload = {
            "account": decoded.account,
            "from_address": decoded.from_address,
            "to_address": decoded.to_address,
            "amount": float(decoded.amount) if decoded.amount is not None else None,
            **decoded.metadata,
        }
        return ChainEvent(
            chain=self.chain,
            block_number=decoded.block_number,
            tx_hash=decoded.tx_hash,
            log_index=decoded.log_index,
            event_type=decoded.event_type,
            payload=payload,
            observed_at=observed_at,
        )

    @staticmethod
    def _hex_int(value: object, field: str) -> int:
        if not isinstance(value, str):
            raise EvmScanError(f"{field} must be a hex string")
        try:
            return int(value, 16)
        except ValueError as exc:
            raise EvmScanError(f"invalid {field}: {value!r}") from exc


class EvmSnapshotReader:
    def __init__(
        self,
        chain: str,
        rpc: RpcClient,
        token_address: str,
        *,
        watched_addresses: set[str] | None = None,
    ) -> None:
        self.chain = chain
        self._rpc = rpc
        self._token_address = token_address
        self._watched_addresses = {
            address.lower() for address in (watched_addresses or set())
        }

    async def read(self, safe_block: int, collected_at: datetime) -> EvmSnapshot:
        block_tag = hex(safe_block)
        implementation_raw = await self._rpc.call(
            "eth_getStorageAt",
            [self._token_address, IMPLEMENTATION_SLOT, block_tag],
        )
        admin_raw = await self._rpc.call(
            "eth_getStorageAt", [self._token_address, ADMIN_SLOT, block_tag]
        )
        implementation = self._decode_storage_address(
            implementation_raw, "implementation"
        )
        admin = self._decode_storage_address(admin_raw, "admin")
        code = await self._rpc.call("eth_getCode", [implementation, block_tag])
        if not isinstance(code, str) or not code.startswith("0x"):
            raise EvmScanError("implementation code must be hex")
        try:
            code_hash = "0x" + keccak(hexstr=code).hex()
        except ValueError as exc:
            raise EvmScanError("implementation code must be hex") from exc

        owner = await self._optional_address_call(OWNER_SELECTOR, block_tag)
        paused = await self._optional_bool_call(PAUSED_SELECTOR, block_tag)
        frozen_accounts = {
            address: await self._frozen(address, block_tag)
            for address in sorted(self._watched_addresses)
        }
        observations = self._observations(
            safe_block,
            implementation,
            admin,
            code_hash,
            owner,
            paused,
            collected_at,
        )
        return EvmSnapshot(
            self.chain,
            safe_block,
            implementation,
            admin,
            code_hash,
            owner,
            paused,
            tuple(observations),
            frozen_accounts=frozen_accounts,
        )

    async def _frozen(self, address: str, block_tag: str) -> bool:
        data = FROZEN_SELECTOR + "0" * 24 + address.removeprefix("0x")
        value = await self._rpc.call(
            "eth_call", [{"to": self._token_address, "data": data}, block_tag]
        )
        return self._decode_bool(value, "frozen")

    async def _optional_address_call(
        self, selector: str, block_tag: str
    ) -> str | None:
        try:
            value = await self._rpc.call(
                "eth_call",
                [{"to": self._token_address, "data": selector}, block_tag],
            )
        except RpcResponseError:
            return None
        return self._decode_storage_address(value, "owner")

    async def _optional_bool_call(
        self, selector: str, block_tag: str
    ) -> bool | None:
        try:
            value = await self._rpc.call(
                "eth_call",
                [{"to": self._token_address, "data": selector}, block_tag],
            )
        except RpcResponseError:
            return None
        return self._decode_bool(value, "paused")

    @staticmethod
    def _decode_bool(value: object, field_name: str) -> bool:
        if not isinstance(value, str) or not value.startswith("0x"):
            raise EvmScanError(f"{field_name} result must be hex")
        try:
            parsed = int(value, 16)
        except ValueError as exc:
            raise EvmScanError(f"{field_name} result must be hex") from exc
        if parsed not in (0, 1):
            raise EvmScanError(f"{field_name} result must be ABI bool")
        return bool(parsed)

    @staticmethod
    def _decode_storage_address(value: object, field: str) -> str:
        if not isinstance(value, str) or not value.startswith("0x"):
            raise EvmScanError(f"{field} result must be hex")
        raw = value[2:]
        if len(raw) != 64:
            raise EvmScanError(f"{field} result must be 32 bytes")
        try:
            int(raw, 16)
        except ValueError as exc:
            raise EvmScanError(f"{field} result must be hex") from exc
        return "0x" + raw[-40:]

    def _observations(
        self,
        block_number: int,
        implementation: str,
        admin: str,
        code_hash: str,
        owner: str | None,
        paused: bool | None,
        collected_at: datetime,
    ) -> list[object]:
        from usd1_monitor.models import Observation

        common = {
            "source": "evm_rpc",
            "scope": self.chain,
            "observed_at": collected_at,
            "collected_at": collected_at,
        }
        return [
            Observation("evm.implementation", value=1, unit="address", metadata={"address": implementation, "block": block_number}, **common),
            Observation("evm.admin", value=1, unit="address", metadata={"address": admin, "block": block_number}, **common),
            Observation("evm.code_hash", value=1, unit="hash", metadata={"hash": code_hash, "block": block_number}, **common),
            Observation("evm.owner", value=1 if owner else 0, unit="address", metadata={"address": owner, "supported": owner is not None, "block": block_number}, **common),
            Observation("evm.paused", value=float(paused) if paused is not None else 0, unit="bool", metadata={"supported": paused is not None, "block": block_number}, **common),
        ]


KNOWN_PRIVILEGED_SELECTORS = {
    "0x8456cb59": "PAUSE",
    "0x3f4ba83a": "UNPAUSE",
    "0xf2fde38b": "TRANSFER_OWNERSHIP",
    "0x8f283970": "CHANGE_ADMIN",
    "0x3659cfe6": "UPGRADE_TO",
    "0x4f1ef286": "UPGRADE_TO_AND_CALL",
    "0x99a88ec4": "PROXY_ADMIN_UPGRADE",
    "0x9623609d": "PROXY_ADMIN_UPGRADE_AND_CALL",
}
SAFE_EXEC_TRANSACTION_SELECTOR = "0x6a761202"
SAFE_EXECUTION_SUCCESS_TOPIC = "0x" + keccak(
    text="ExecutionSuccess(bytes32,uint256)"
).hex()


class PrivilegedCallCollector:
    def __init__(
        self,
        chain: str,
        rpc: RpcClient,
        token_address: str,
        *,
        max_concurrency: int = 8,
        block_batch_size: int = 100,
    ) -> None:
        if max_concurrency < 1 or block_batch_size < 1:
            raise ValueError("EVM concurrency and batch size must be positive")
        self.chain = chain
        self._rpc = rpc
        self._token_address = token_address.lower()
        self._max_concurrency = max_concurrency
        self._block_batch_size = block_batch_size
        self.last_block_hashes: dict[int, str] = {}

    async def collect(
        self,
        start_block: int,
        end_block: int,
        *,
        admin: str,
        owner: str | None,
        decoded_events: list[DecodedEvent],
    ) -> list[DecodedEvent]:
        self.last_block_hashes = {}
        transfers_by_tx: dict[str, list[DecodedEvent]] = {}
        for event in decoded_events:
            if event.event_type == "TRANSFER":
                transfers_by_tx.setdefault(event.tx_hash.lower(), []).append(event)

        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def fetch_block(
            block_number: int,
        ) -> tuple[object, str, str | None, str | None]:
            async with semaphore:
                block = await self._rpc.call(
                    "eth_getBlockByNumber", [hex(block_number), True]
                )
                if block_number == 0:
                    return block, admin.lower(), owner.lower() if owner else None, None
                block_tag = hex(block_number - 1)
                admin_raw = await self._rpc.call(
                    "eth_getStorageAt",
                    [self._token_address, ADMIN_SLOT, block_tag],
                )
                block_admin = EvmSnapshotReader._decode_storage_address(
                    admin_raw, "historical admin"
                ).lower()
                block_owner = await self._read_owner(self._token_address, block_tag)
                block_admin_owner = await self._read_owner(block_admin, block_tag)
                return block, block_admin, block_owner, block_admin_owner

        facts: list[DecodedEvent] = []
        for batch_start in range(
            start_block, end_block + 1, self._block_batch_size
        ):
            batch_end = min(end_block, batch_start + self._block_batch_size - 1)
            async with asyncio.TaskGroup() as group:
                tasks = [
                    (block_number, group.create_task(fetch_block(block_number)))
                    for block_number in range(batch_start, batch_end + 1)
                ]

            for block_number, task in tasks:
                block, current_admin, current_owner, current_admin_owner = task.result()
                if not isinstance(block, dict) or not isinstance(
                    block.get("transactions"), list
                ):
                    raise EvmScanError("full block response is malformed")
                block_hash = block.get("hash")
                if (
                    not isinstance(block_hash, str)
                    or re.fullmatch(r"0x[0-9a-fA-F]{64}", block_hash) is None
                ):
                    raise EvmScanError("full block response has invalid block hash")
                self.last_block_hashes[block_number] = block_hash
                for tx in block["transactions"]:
                    if not isinstance(tx, dict):
                        raise EvmScanError("transaction response is malformed")
                    sender = str(tx.get("from", "")).lower()
                    destination = str(tx.get("to", "")).lower()
                    input_data = str(tx.get("input", ""))
                    tx_hash = str(tx.get("hash", ""))
                    transfers = transfers_by_tx.get(tx_hash.lower(), [])
                    effective_privileged = {
                        identity
                        for identity in (
                            current_admin,
                            current_owner,
                            current_admin_owner,
                        )
                        if identity is not None
                    }
                    third_party_move = any(
                        event.from_address is not None
                        and event.to_address is not None
                        and event.from_address.lower() != ZERO_ADDRESS
                        and event.to_address.lower() != ZERO_ADDRESS
                        and event.from_address.lower() not in effective_privileged
                        for event in transfers
                    )
                    effective_destinations = {self._token_address, current_admin}
                    selector = input_data[:10].lower()
                    known = KNOWN_PRIVILEGED_SELECTORS.get(selector)
                    call_destination = destination
                    call_input = input_data
                    direct_call = (
                        sender in effective_privileged
                        and destination in effective_destinations
                    )
                    safe_execution = False
                    if (
                        selector == SAFE_EXEC_TRANSACTION_SELECTOR
                        and destination in effective_privileged
                        and await self._is_contract(destination, block_number)
                    ):
                        decoded_safe_call = self._decode_safe_call(input_data)
                        if decoded_safe_call is not None:
                            inner_destination, inner_input = decoded_safe_call
                            if inner_destination in effective_destinations:
                                safe_execution = True
                                call_destination = inner_destination
                                call_input = inner_input
                                selector = inner_input[:10].lower()
                                known = KNOWN_PRIVILEGED_SELECTORS.get(selector)
                    admin_contract_call = False
                    if known is not None and destination == current_admin:
                        admin_contract_call = (
                            sender in effective_privileged
                            or await self._is_contract(destination, block_number)
                        )
                    known_privileged_target_call = (
                        known is not None
                        and (
                            destination == self._token_address
                            or admin_contract_call
                            or safe_execution
                        )
                    )
                    if input_data in ("", "0x") or not (
                        direct_call
                        or safe_execution
                        or known_privileged_target_call
                    ):
                        continue
                    receipt = await self._rpc.call(
                        "eth_getTransactionReceipt", [tx_hash]
                    )
                    if not isinstance(receipt, dict) or "status" not in receipt:
                        raise EvmScanError("transaction receipt is malformed")
                    try:
                        succeeded = int(str(receipt["status"]), 16) == 1
                    except ValueError as exc:
                        raise EvmScanError(
                            "transaction receipt status must be hex"
                        ) from exc
                    if not succeeded:
                        continue
                    if safe_execution and not self._receipt_has_topic(
                        receipt, SAFE_EXECUTION_SUCCESS_TOPIC
                    ):
                        continue
                    if known in {"TRANSFER_OWNERSHIP", "CHANGE_ADMIN"}:
                        new_identity = self._decode_address_argument(call_input)
                        if new_identity is not None:
                            if (
                                known == "CHANGE_ADMIN"
                                and call_destination == self._token_address
                            ):
                                current_admin = new_identity
                                current_admin_owner = None
                            elif known == "TRANSFER_OWNERSHIP":
                                if call_destination == self._token_address:
                                    current_owner = new_identity
                                elif call_destination == current_admin:
                                    current_admin_owner = new_identity
                    facts.append(
                        DecodedEvent(
                            chain=self.chain,
                            event_type=(
                                f"PRIVILEGED_{known}"
                                if known
                                else "PRIVILEGED_UNKNOWN_CALL"
                            ),
                            tx_hash=tx_hash,
                            log_index=-1,
                            block_number=block_number,
                            metadata={
                                "sender": sender,
                                "to": call_destination,
                                "executor": destination,
                                "selector": selector,
                                "input": call_input,
                                "third_party_asset_move": third_party_move,
                            },
                        )
                    )
        return facts

    async def _is_contract(self, target: str, block_number: int) -> bool:
        raw = await self._rpc.call("eth_getCode", [target, hex(block_number)])
        if not isinstance(raw, str) or not raw.startswith("0x"):
            raise EvmScanError("contract code must be hex")
        try:
            return bool(raw[2:]) and int(raw[2:], 16) != 0
        except ValueError as exc:
            raise EvmScanError("contract code must be hex") from exc

    async def _read_owner(self, target: str, block_tag: str) -> str | None:
        try:
            raw = await self._rpc.call(
                "eth_call",
                [{"to": target, "data": OWNER_SELECTOR}, block_tag],
            )
        except RpcResponseError:
            return None
        if not isinstance(raw, str) or len(raw) < 42:
            return None
        address = "0x" + raw[-40:].lower()
        return address if int(address[2:], 16) != 0 else None

    @staticmethod
    def _decode_safe_call(input_data: str) -> tuple[str, str] | None:
        if not input_data.startswith(SAFE_EXEC_TRANSACTION_SELECTOR):
            return None
        arguments = input_data[10:]
        if len(arguments) < 10 * 64:
            return None
        try:
            target_word = arguments[:64]
            data_offset = int(arguments[128:192], 16)
            data_length_start = data_offset * 2
            data_length = int(
                arguments[data_length_start : data_length_start + 64], 16
            )
        except ValueError:
            return None
        data_start = data_length_start + 64
        data_end = data_start + data_length * 2
        if data_offset < 10 * 32 or data_end > len(arguments):
            return None
        target = "0x" + target_word[-40:].lower()
        if int(target[2:], 16) == 0:
            return None
        return target, "0x" + arguments[data_start:data_end].lower()

    @staticmethod
    def _receipt_has_topic(receipt: dict, expected_topic: str) -> bool:
        logs = receipt.get("logs")
        if not isinstance(logs, list):
            return False
        for item in logs:
            if not isinstance(item, dict):
                continue
            topics = item.get("topics")
            if (
                isinstance(topics, list)
                and topics
                and str(topics[0]).lower() == expected_topic
            ):
                return True
        return False

    @staticmethod
    def _decode_address_argument(input_data: str) -> str | None:
        if len(input_data) < 74:
            return None
        raw = input_data[10:74]
        try:
            value = int(raw, 16)
        except ValueError:
            return None
        if value == 0:
            return None
        return "0x" + raw[-40:].lower()
