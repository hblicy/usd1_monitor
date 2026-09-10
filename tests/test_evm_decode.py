import pytest

from usd1_monitor.evm_abi import EvmDecodeError, decode_log, event_topic


def test_zero_address_transfer_is_mint() -> None:
    log = {
        "topics": [
            event_topic("Transfer(address,address,uint256)"),
            "0x" + "00" * 32,
            "0x" + "00" * 12 + "11" * 20,
        ],
        "data": hex(10_000_000 * 10**18),
        "transactionHash": "0xabc",
        "logIndex": "0x1",
        "blockNumber": "0x10",
    }

    event = decode_log("ethereum", log, decimals=18)

    assert event.event_type == "MINT"
    assert event.amount == 10_000_000


def test_freeze_extracts_target_address() -> None:
    target = "22" * 20
    log = {
        "topics": [
            event_topic("Freeze(address,address)"),
            "0x" + "00" * 12 + "33" * 20,
            "0x" + "00" * 12 + target,
        ],
        "data": "0x",
        "transactionHash": "0xdef",
        "logIndex": "0x0",
        "blockNumber": "0x20",
    }

    assert decode_log("bsc", log, decimals=18).account.lower().endswith(target)


def test_unknown_topic_is_retained_as_unknown_log() -> None:
    event = decode_log(
        "ethereum",
        {
            "topics": ["0x" + "12" * 32],
            "data": "0x1234",
            "transactionHash": "0xabc",
            "logIndex": "0x0",
            "blockNumber": "0x10",
        },
        decimals=18,
    )

    assert event.event_type == "UNKNOWN_LOG"
    assert event.metadata["data"] == "0x1234"


def test_malformed_known_log_raises() -> None:
    with pytest.raises(EvmDecodeError, match="Transfer"):
        decode_log(
            "ethereum",
            {
                "topics": [event_topic("Transfer(address,address,uint256)")],
                "data": "0x",
                "transactionHash": "0xabc",
                "logIndex": "0x0",
                "blockNumber": "0x10",
            },
            decimals=18,
        )


def test_decodes_upgraded_event() -> None:
    implementation = "0x" + "11" * 20
    event = decode_log(
        "ethereum",
        {
            "topics": [
                event_topic("Upgraded(address)"),
                "0x" + "00" * 12 + implementation[2:],
            ],
            "data": "0x",
            "transactionHash": "0xupgrade",
            "logIndex": "0x0",
            "blockNumber": "0x10",
        },
        decimals=18,
    )

    assert event.event_type == "IMPLEMENTATION_CHANGED"
    assert event.to_address == implementation


def test_decodes_admin_changed_event() -> None:
    previous = "0x" + "22" * 20
    current = "0x" + "33" * 20
    event = decode_log(
        "ethereum",
        {
            "topics": [event_topic("AdminChanged(address,address)")],
            "data": (
                "0x"
                + "00" * 12
                + previous[2:]
                + "00" * 12
                + current[2:]
            ),
            "transactionHash": "0xadmin",
            "logIndex": "0x1",
            "blockNumber": "0x10",
        },
        decimals=18,
    )

    assert event.event_type == "ADMIN_CHANGED"
    assert event.from_address == previous
    assert event.to_address == current
