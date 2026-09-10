from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from eth_utils import keccak


ZERO_ADDRESS = "0x" + "00" * 20


class EvmDecodeError(ValueError):
    pass


@dataclass(frozen=True)
class DecodedEvent:
    chain: str
    event_type: str
    tx_hash: str
    log_index: int
    block_number: int
    account: str | None = None
    from_address: str | None = None
    to_address: str | None = None
    amount: Decimal | int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def event_topic(signature: str) -> str:
    return "0x" + keccak(text=signature).hex()


EVENTS = {
    event_topic("Transfer(address,address,uint256)"): "Transfer",
    event_topic("Mint(address,address,uint256)"): "Mint",
    event_topic("Burn(address,address,uint256)"): "Burn",
    event_topic("Freeze(address,address)"): "Freeze",
    event_topic("Unfreeze(address,address)"): "Unfreeze",
    event_topic("Paused(address)"): "Paused",
    event_topic("Unpaused(address)"): "Unpaused",
    event_topic("OwnershipTransferred(address,address)"): "OwnershipTransferred",
    event_topic("Upgraded(address)"): "Upgraded",
    event_topic("AdminChanged(address,address)"): "AdminChanged",
}


def _address(topic: object, event_name: str) -> str:
    if not isinstance(topic, str) or not topic.startswith("0x"):
        raise EvmDecodeError(f"malformed {event_name} address topic")
    raw = topic[2:]
    if len(raw) != 64:
        raise EvmDecodeError(f"malformed {event_name} address topic")
    try:
        int(raw, 16)
    except ValueError as exc:
        raise EvmDecodeError(f"malformed {event_name} address topic") from exc
    return "0x" + raw[-40:]


def _uint(data: object, event_name: str) -> int:
    if not isinstance(data, str) or not data.startswith("0x") or data == "0x":
        raise EvmDecodeError(f"malformed {event_name} data")
    try:
        return int(data, 16)
    except ValueError as exc:
        raise EvmDecodeError(f"malformed {event_name} data") from exc


def _data_address(data: object, word: int, event_name: str) -> str:
    if not isinstance(data, str) or not data.startswith("0x"):
        raise EvmDecodeError(f"malformed {event_name} data")
    raw = data[2:]
    if len(raw) != 128:
        raise EvmDecodeError(f"malformed {event_name} data")
    start = word * 64
    return _address("0x" + raw[start : start + 64], event_name)


def decode_log(chain: str, log: object, *, decimals: int) -> DecodedEvent:
    if not isinstance(log, dict):
        raise EvmDecodeError("log must be an object")
    try:
        topics = log["topics"]
        data = log["data"]
        tx_hash = log["transactionHash"]
        log_index = int(log["logIndex"], 16)
        block_number = int(log["blockNumber"], 16)
    except (KeyError, TypeError, ValueError) as exc:
        raise EvmDecodeError("log identity fields are malformed") from exc
    if not isinstance(topics, list) or not topics or not isinstance(tx_hash, str):
        raise EvmDecodeError("log topics or transaction hash are malformed")

    topic0 = str(topics[0]).lower()
    event_name = EVENTS.get(topic0)
    if event_name is None:
        return DecodedEvent(
            chain,
            "UNKNOWN_LOG",
            tx_hash,
            log_index,
            block_number,
            metadata={"topics": topics, "data": data},
        )

    try:
        if event_name == "Transfer":
            if len(topics) != 3:
                raise EvmDecodeError("malformed Transfer topics")
            source = _address(topics[1], event_name)
            target = _address(topics[2], event_name)
            amount = Decimal(_uint(data, event_name)) / Decimal(10**decimals)
            kind = (
                "MINT"
                if source.lower() == ZERO_ADDRESS
                else "BURN"
                if target.lower() == ZERO_ADDRESS
                else "TRANSFER"
            )
            return DecodedEvent(
                chain,
                kind,
                tx_hash,
                log_index,
                block_number,
                from_address=source,
                to_address=target,
                amount=amount,
            )
        if event_name in {"Mint", "Burn"}:
            if len(topics) != 3:
                raise EvmDecodeError(f"malformed {event_name} topics")
            account = _address(topics[2], event_name)
            amount = Decimal(_uint(data, event_name)) / Decimal(10**decimals)
            return DecodedEvent(
                chain,
                event_name.upper(),
                tx_hash,
                log_index,
                block_number,
                account=account,
                amount=amount,
                metadata={"operator": _address(topics[1], event_name)},
            )
        if event_name in {"Freeze", "Unfreeze"}:
            if len(topics) != 3:
                raise EvmDecodeError(f"malformed {event_name} topics")
            return DecodedEvent(
                chain,
                event_name.upper(),
                tx_hash,
                log_index,
                block_number,
                account=_address(topics[2], event_name),
                metadata={"operator": _address(topics[1], event_name)},
            )
        if event_name in {"Paused", "Unpaused"}:
            if len(topics) != 2:
                raise EvmDecodeError(f"malformed {event_name} topics")
            return DecodedEvent(
                chain,
                event_name.upper(),
                tx_hash,
                log_index,
                block_number,
                account=_address(topics[1], event_name),
            )
        if event_name == "OwnershipTransferred":
            if len(topics) != 3:
                raise EvmDecodeError("malformed OwnershipTransferred topics")
            return DecodedEvent(
                chain,
                "OWNER_CHANGED",
                tx_hash,
                log_index,
                block_number,
                from_address=_address(topics[1], event_name),
                to_address=_address(topics[2], event_name),
            )
        if event_name == "Upgraded":
            if len(topics) != 2 or data != "0x":
                raise EvmDecodeError("malformed Upgraded log")
            return DecodedEvent(
                chain,
                "IMPLEMENTATION_CHANGED",
                tx_hash,
                log_index,
                block_number,
                to_address=_address(topics[1], event_name),
            )
        if event_name == "AdminChanged":
            if len(topics) != 1:
                raise EvmDecodeError("malformed AdminChanged topics")
            return DecodedEvent(
                chain,
                "ADMIN_CHANGED",
                tx_hash,
                log_index,
                block_number,
                from_address=_data_address(data, 0, event_name),
                to_address=_data_address(data, 1, event_name),
            )
    except EvmDecodeError:
        raise
    except (TypeError, ValueError) as exc:
        raise EvmDecodeError(f"malformed {event_name} log") from exc
    raise EvmDecodeError(f"unsupported known event {event_name}")
