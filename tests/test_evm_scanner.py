import pytest

from tests.fakes import FakeRpc
from usd1_monitor.collectors.evm import EvmScanError, EvmScanner, scan_range


def test_scan_range_uses_safe_head_and_overlap() -> None:
    assert scan_range(130, 3, 100, 20) == (81, 127)
    assert scan_range(2, 3, None, 20) is None


def test_scanner_rejects_batch_that_cannot_advance_past_overlap(storage) -> None:
    with pytest.raises(ValueError, match="batch_blocks must be larger"):
        EvmScanner(
            "ethereum",
            FakeRpc(),
            storage,
            confirmation_depth=3,
            overlap_blocks=20,
            batch_blocks=20,
        )


@pytest.mark.asyncio
async def test_scanner_uses_safe_head_and_overlap_without_advancing_cursor(storage) -> None:
    fake_rpc = FakeRpc()
    await storage.set_scan_cursor("ethereum", 100)
    fake_rpc.result("eth_blockNumber", "0x82")
    for _ in range(5):
        fake_rpc.result("eth_getLogs", [])
    scanner = EvmScanner(
        "ethereum",
        fake_rpc,
        storage,
        confirmation_depth=3,
        overlap_blocks=20,
        batch_blocks=100,
    )

    result = await scanner.scan_once()

    requests = [params[0] for params in fake_rpc.calls_for("eth_getLogs")]
    assert requests[0]["fromBlock"] == hex(81)
    assert requests[-1]["toBlock"] == hex(127)
    assert await storage.get_scan_cursor("ethereum") == 100
    assert result.safe_head == 127


@pytest.mark.asyncio
async def test_scanner_caps_each_catch_up_cycle_to_one_batch(storage) -> None:
    fake_rpc = FakeRpc()
    await storage.set_scan_cursor("ethereum", 100)
    fake_rpc.result("eth_blockNumber", hex(10_000))
    for _ in range(10):
        fake_rpc.result("eth_getLogs", [])
    scanner = EvmScanner(
        "ethereum",
        fake_rpc,
        storage,
        confirmation_depth=3,
        overlap_blocks=20,
        batch_blocks=100,
    )

    result = await scanner.scan_once()

    requests = [params[0] for params in fake_rpc.calls_for("eth_getLogs")]
    assert requests[0]["fromBlock"] == hex(81)
    assert requests[-1]["toBlock"] == hex(180)
    assert result.safe_head == 9_997
    assert result.cursor == 180


@pytest.mark.asyncio
async def test_scanner_chunks_log_queries_without_reducing_cycle_progress(
    storage,
) -> None:
    fake_rpc = FakeRpc()
    await storage.set_scan_cursor("ethereum", 100)
    fake_rpc.result("eth_blockNumber", hex(10_000))
    for _ in range(10):
        fake_rpc.result("eth_getLogs", [])
    scanner = EvmScanner(
        "ethereum",
        fake_rpc,
        storage,
        confirmation_depth=3,
        overlap_blocks=20,
        batch_blocks=100,
    )

    result = await scanner.scan_once()

    requests = [params[0] for params in fake_rpc.calls_for("eth_getLogs")]
    assert [
        (request["fromBlock"], request["toBlock"]) for request in requests
    ] == [
        (hex(start), hex(start + 9))
        for start in range(81, 181, 10)
    ]
    assert result.cursor == 180


@pytest.mark.asyncio
async def test_scanner_chunks_two_thousand_blocks_into_four_queries(
    storage,
) -> None:
    fake_rpc = FakeRpc()
    await storage.set_scan_cursor("bsc", 100)
    fake_rpc.result("eth_blockNumber", hex(2_090))
    for _ in range(4):
        fake_rpc.result("eth_getLogs", [])
    scanner = EvmScanner(
        "bsc",
        fake_rpc,
        storage,
        confirmation_depth=10,
        overlap_blocks=20,
        batch_blocks=2_000,
        log_query_chunk_blocks=500,
    )

    result = await scanner.scan_once()

    requests = [params[0] for params in fake_rpc.calls_for("eth_getLogs")]
    assert [
        (request["fromBlock"], request["toBlock"]) for request in requests
    ] == [
        (hex(81), hex(580)),
        (hex(581), hex(1_080)),
        (hex(1_081), hex(1_580)),
        (hex(1_581), hex(2_080)),
    ]
    assert result.cursor == result.safe_head == 2_080


@pytest.mark.asyncio
async def test_scanner_rejects_safe_head_behind_persisted_cursor(storage) -> None:
    fake_rpc = FakeRpc()
    await storage.set_scan_cursor("ethereum", 100)
    fake_rpc.result("eth_blockNumber", hex(90))
    scanner = EvmScanner(
        "ethereum",
        fake_rpc,
        storage,
        confirmation_depth=3,
        overlap_blocks=20,
        batch_blocks=100,
    )

    with pytest.raises(EvmScanError, match="behind persisted cursor"):
        await scanner.scan_once()

    assert fake_rpc.calls_for("eth_getLogs") == []


@pytest.mark.asyncio
async def test_scanner_does_not_write_events_or_cursor(storage) -> None:
    fake_rpc = FakeRpc()
    fake_rpc.result("eth_blockNumber", "0x20")
    fake_rpc.result(
        "eth_getLogs",
        [
            {
                "transactionHash": "0x01",
                "logIndex": "0x0",
                "blockNumber": "0x10",
                "topics": ["0xabc"],
                "data": "0x",
            }
        ],
    )
    fake_rpc.result("eth_getLogs", [])
    scanner = EvmScanner(
        "ethereum",
        fake_rpc,
        storage,
        confirmation_depth=3,
        overlap_blocks=20,
        batch_blocks=100,
    )

    result = await scanner.scan_once()

    assert await storage.get_scan_cursor("ethereum") is None
    assert await storage.count_chain_events() == 0
    assert len(result.new_events) == 1


@pytest.mark.asyncio
async def test_replayed_log_is_idempotent(storage) -> None:
    raw_log = {
        "transactionHash": "0x01",
        "logIndex": "0x0",
        "blockNumber": "0x10",
        "topics": ["0xabc"],
        "data": "0x",
    }
    fake_rpc = FakeRpc()
    fake_rpc.result("eth_blockNumber", "0x20")
    fake_rpc.result("eth_getLogs", [raw_log])
    fake_rpc.result("eth_getLogs", [])
    fake_rpc.result("eth_blockNumber", "0x20")
    fake_rpc.result("eth_getLogs", [raw_log])
    fake_rpc.result("eth_getLogs", [])
    scanner = EvmScanner(
        "ethereum",
        fake_rpc,
        storage,
        confirmation_depth=3,
        overlap_blocks=20,
        batch_blocks=100,
    )

    first = await scanner.scan_once()
    second = await scanner.scan_once()

    assert len(first.new_events) == len(second.new_events) == 1
    assert await storage.count_chain_events() == 0
    assert await storage.get_scan_cursor("ethereum") is None
