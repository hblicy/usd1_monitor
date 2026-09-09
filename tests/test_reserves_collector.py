from datetime import UTC, datetime

import pytest
from eth_abi import encode

from tests.fakes import FakeRpc
from usd1_monitor.collectors.reserves import (
    PorCollector,
    PorDataError,
    decode_por_calls,
    selector,
)


COLLECTED_AT = datetime(2026, 9, 7, 4, 0, tzinfo=UTC)
ORACLE_TIMESTAMP = int(COLLECTED_AT.timestamp()) - 60
ORACLE = "0x" + "11" * 20


def rpc_bytes(value: bytes) -> str:
    return "0x" + encode(["bytes"], [value]).hex()


def valid_calls(timestamp: int = ORACLE_TIMESTAMP):
    bundle = encode(
        ["uint256", "uint256"],
        [timestamp, 4_250_000_000 * 10**18],
    )
    return (
        rpc_bytes(bundle),
        "0x" + encode(["uint256"], [timestamp]).hex(),
        "0x" + encode(["uint8[]"], [[18]]).hex(),
    )


def test_decode_por_bundle_checks_timestamp_and_decimals() -> None:
    latest_bundle, latest_timestamp, bundle_decimals = valid_calls()

    result = decode_por_calls(
        latest_bundle=latest_bundle,
        latest_timestamp=latest_timestamp,
        bundle_decimals=bundle_decimals,
        collected_at=COLLECTED_AT,
    )

    assert result.reserves == 4_250_000_000
    assert result.oracle_timestamp == ORACLE_TIMESTAMP


def test_decode_por_bundle_rejects_timestamp_mismatch() -> None:
    latest_bundle, _, bundle_decimals = valid_calls(ORACLE_TIMESTAMP)
    latest_timestamp = "0x" + encode(
        ["uint256"], [ORACLE_TIMESTAMP + 1]
    ).hex()

    with pytest.raises(PorDataError, match="timestamp mismatch"):
        decode_por_calls(
            latest_bundle=latest_bundle,
            latest_timestamp=latest_timestamp,
            bundle_decimals=bundle_decimals,
            collected_at=COLLECTED_AT,
        )


@pytest.mark.parametrize(
    "calls, message",
    [
        (("0x12", "0x12", "0x12"), "malformed"),
        (valid_calls(0), "positive"),
        (valid_calls(int(COLLECTED_AT.timestamp()) + 301), "future"),
        (
            (
                valid_calls()[0],
                valid_calls()[1],
                "0x" + encode(["uint8[]"], [[]]).hex(),
            ),
            "reserves entry",
        ),
    ],
)
def test_rejects_invalid_or_inconsistent_bundle(calls, message: str) -> None:
    with pytest.raises(PorDataError, match=message):
        decode_por_calls(
            latest_bundle=calls[0],
            latest_timestamp=calls[1],
            bundle_decimals=calls[2],
            collected_at=COLLECTED_AT,
        )


@pytest.mark.asyncio
async def test_collector_reads_all_values_at_same_confirmed_block() -> None:
    rpc = FakeRpc()
    for value in valid_calls():
        rpc.result("eth_call", value)
    collector = PorCollector(rpc, ORACLE)

    snapshot = await collector.collect(123, COLLECTED_AT)

    assert snapshot.reserves == 4_250_000_000
    assert rpc.calls_for("eth_call") == [
        [{"to": ORACLE, "data": selector("latestBundle()")}, hex(123)],
        [
            {"to": ORACLE, "data": selector("latestBundleTimestamp()")},
            hex(123),
        ],
        [{"to": ORACLE, "data": selector("bundleDecimals()")}, hex(123)],
    ]
    assert {item.metric for item in snapshot.observations} == {
        "por.reserves",
        "por.oracle_age_seconds",
    }
    assert snapshot.observations[0].metadata["source_urls"] == [
        f"https://etherscan.io/address/{ORACLE}#readContract",
        "https://etherscan.io/block/123",
    ]
