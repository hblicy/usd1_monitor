import asyncio
from datetime import UTC, datetime

import pytest
from eth_utils import keccak

from tests.fakes import FakeRpc
from usd1_monitor.collectors.evm import (
    ADMIN_SLOT,
    IMPLEMENTATION_SLOT,
    EvmScanError,
    EvmSnapshotReader,
    PrivilegedCallCollector,
)
from usd1_monitor.evm_abi import DecodedEvent
from usd1_monitor.rpc import RpcError, RpcResponseError


NOW = datetime(2026, 9, 7, 4, 0, tzinfo=UTC)
TOKEN = "0x" + "aa" * 20
IMPLEMENTATION = "0x" + "bb" * 20
ADMIN = "0x" + "cc" * 20
OWNER = "0x" + "dd" * 20
WATCHED = "0x" + "11" * 20
BLOCK_HASH = "0x" + "ab" * 32


def encoded_address(address: str) -> str:
    return "0x" + "00" * 12 + address[2:]


def safe_exec_input(target: str, inner_data: str) -> str:
    raw_data = inner_data.removeprefix("0x")
    padded_data = raw_data.ljust(((len(raw_data) + 63) // 64) * 64, "0")
    words = [
        "0" * 24 + target.removeprefix("0x"),
        "0" * 64,
        f"{320:064x}",
        *("0" * 64 for _ in range(7)),
        f"{len(raw_data) // 2:064x}",
        padded_data,
    ]
    return "0x6a761202" + "".join(words)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "block",
    [
        {"transactions": []},
        {"hash": "0x1234", "transactions": []},
        {"hash": "0x" + "gg" * 32, "transactions": []},
        {
            "hash": "0x" + "a" * 31 + "_" + "a" * 32,
            "transactions": [],
        },
        {"hash": "0x" + "a" * 63 + "٠", "transactions": []},
    ],
)
async def test_privileged_scan_rejects_invalid_block_hash(block) -> None:
    rpc = FakeRpc()
    rpc.result("eth_getBlockByNumber", block)

    with pytest.raises(EvmScanError, match="block hash"):
        await PrivilegedCallCollector("ethereum", rpc, TOKEN).collect(
            0, 0, admin=ADMIN, owner=None, decoded_events=[]
        )


EXECUTION_SUCCESS_TOPIC = "0x" + keccak(
    text="ExecutionSuccess(bytes32,uint256)"
).hex()
EXECUTION_FAILURE_TOPIC = "0x" + keccak(
    text="ExecutionFailure(bytes32,uint256)"
).hex()


@pytest.mark.asyncio
async def test_snapshot_reads_slots_code_owner_and_pause_at_safe_block() -> None:
    rpc = FakeRpc()
    rpc.result("eth_getStorageAt", encoded_address(IMPLEMENTATION))
    rpc.result("eth_getStorageAt", encoded_address(ADMIN))
    rpc.result("eth_getCode", "0x60016000")
    rpc.result("eth_call", encoded_address(OWNER))
    rpc.result("eth_call", RpcResponseError("admin has no owner"))
    rpc.result("eth_call", "0x" + "00" * 31 + "01")

    snapshot = await EvmSnapshotReader("ethereum", rpc, TOKEN).read(100, NOW)

    assert snapshot.implementation == IMPLEMENTATION
    assert snapshot.admin == ADMIN
    assert snapshot.owner == OWNER
    assert snapshot.paused is True
    assert snapshot.code_hash == "0x" + keccak(hexstr="0x60016000").hex()
    assert rpc.calls_for("eth_getStorageAt") == [
        [TOKEN, IMPLEMENTATION_SLOT, hex(100)],
        [TOKEN, ADMIN_SLOT, hex(100)],
    ]
    assert all(call[-1] == hex(100) for call in rpc.calls_for("eth_call"))


@pytest.mark.asyncio
async def test_snapshot_reads_watched_frozen_state_at_safe_block() -> None:
    rpc = FakeRpc()
    rpc.result("eth_getStorageAt", encoded_address(IMPLEMENTATION))
    rpc.result("eth_getStorageAt", encoded_address(ADMIN))
    rpc.result("eth_getCode", "0x60016000")
    rpc.result("eth_call", encoded_address(OWNER))
    rpc.result("eth_call", RpcResponseError("admin has no owner"))
    rpc.result("eth_call", "0x" + "00" * 32)
    rpc.result("eth_call", "0x" + "00" * 31 + "01")

    snapshot = await EvmSnapshotReader(
        "ethereum", rpc, TOKEN, watched_addresses={WATCHED}
    ).read(100, NOW)

    assert snapshot.frozen_accounts == {WATCHED: True}
    frozen_call = rpc.calls_for("eth_call")[-1]
    assert frozen_call == [
        {"to": TOKEN, "data": "0xd0516650" + "0" * 24 + WATCHED[2:]},
        hex(100),
    ]


@pytest.mark.asyncio
async def test_optional_owner_and_paused_reverts_are_unsupported() -> None:
    rpc = FakeRpc()
    rpc.result("eth_getStorageAt", encoded_address(IMPLEMENTATION))
    rpc.result("eth_getStorageAt", encoded_address(ADMIN))
    rpc.result("eth_getCode", "0x6000")
    rpc.result("eth_call", RpcResponseError("owner reverted"))
    rpc.result("eth_call", RpcResponseError("admin owner reverted"))
    rpc.result("eth_call", RpcResponseError("paused reverted"))

    snapshot = await EvmSnapshotReader("bsc", rpc, TOKEN).read(200, NOW)

    assert snapshot.owner is None
    assert snapshot.paused is None
    by_metric = {item.metric: item for item in snapshot.observations}
    assert by_metric["evm.owner"].metadata["supported"] is False
    assert by_metric["evm.paused"].metadata["supported"] is False


@pytest.mark.asyncio
async def test_snapshot_reads_proxy_admin_owner_at_processed_block() -> None:
    proxy_admin_owner = "0x" + "44" * 20
    rpc = FakeRpc()
    rpc.result("eth_getStorageAt", encoded_address(IMPLEMENTATION))
    rpc.result("eth_getStorageAt", encoded_address(ADMIN))
    rpc.result("eth_getCode", "0x60016000")
    rpc.result("eth_call", encoded_address(OWNER))
    rpc.result("eth_call", encoded_address(proxy_admin_owner))
    rpc.result("eth_call", "0x" + "00" * 32)

    snapshot = await EvmSnapshotReader("ethereum", rpc, TOKEN).read(123, NOW)

    assert snapshot.admin_owner == proxy_admin_owner
    observation = next(
        item
        for item in snapshot.observations
        if item.metric == "evm.admin_owner"
    )
    assert observation.metadata["address"] == proxy_admin_owner
    assert rpc.calls_for("eth_call")[1] == [
        {"to": ADMIN, "data": "0x8da5cb5b"},
        hex(123),
    ]


@pytest.mark.asyncio
async def test_snapshot_treats_empty_admin_owner_result_as_unsupported() -> None:
    rpc = FakeRpc()
    rpc.result("eth_getStorageAt", encoded_address(IMPLEMENTATION))
    rpc.result("eth_getStorageAt", encoded_address(ADMIN))
    rpc.result("eth_getCode", "0x60016000")
    rpc.result("eth_call", encoded_address(OWNER))
    rpc.result("eth_call", "0x")
    rpc.result("eth_call", "0x" + "00" * 32)

    snapshot = await EvmSnapshotReader("ethereum", rpc, TOKEN).read(123, NOW)

    assert snapshot.admin_owner is None
    observation = next(
        item
        for item in snapshot.observations
        if item.metric == "evm.admin_owner"
    )
    assert observation.metadata["supported"] is False


@pytest.mark.asyncio
async def test_optional_call_transport_failure_is_not_treated_as_unsupported() -> None:
    rpc = FakeRpc()
    rpc.result("eth_getStorageAt", "0x" + "00" * 12 + IMPLEMENTATION[2:])
    rpc.result("eth_getStorageAt", "0x" + "00" * 12 + ADMIN[2:])
    rpc.result("eth_getCode", "0x6000")
    rpc.result("eth_call", RpcError("all endpoints offline"))
    reader = EvmSnapshotReader("ethereum", rpc, TOKEN)

    with pytest.raises(RpcError, match="offline"):
        await reader.read(100, NOW)


@pytest.mark.asyncio
async def test_unknown_privileged_call_correlates_third_party_transfer() -> None:
    rpc = FakeRpc()
    rpc.result("eth_getStorageAt", "0x" + "00" * 12 + ADMIN[2:])
    rpc.result("eth_call", "0x" + "00" * 12 + OWNER[2:])
    rpc.result("eth_call", RpcResponseError("admin has no owner"))
    rpc.result(
        "eth_getBlockByNumber",
        {
            "hash": BLOCK_HASH,
            "transactions": [
                {
                    "hash": "0xtx",
                    "from": OWNER,
                    "to": TOKEN,
                    "input": "0x12345678deadbeef",
                }
            ]
        },
    )
    rpc.result("eth_getTransactionReceipt", {"status": "0x1"})
    transfer = DecodedEvent(
        chain="ethereum",
        event_type="TRANSFER",
        tx_hash="0xtx",
        log_index=1,
        block_number=100,
        from_address="0x" + "11" * 20,
        to_address="0x" + "22" * 20,
        amount=1,
    )

    facts = await PrivilegedCallCollector("ethereum", rpc, TOKEN).collect(
        100, 100, admin=ADMIN, owner=OWNER, decoded_events=[transfer]
    )

    assert len(facts) == 1
    assert facts[0].event_type == "PRIVILEGED_UNKNOWN_CALL"
    assert facts[0].metadata["third_party_asset_move"] is True


@pytest.mark.asyncio
async def test_reverted_privileged_call_is_not_recorded() -> None:
    rpc = FakeRpc()
    rpc.result("eth_getStorageAt", "0x" + "00" * 12 + ADMIN[2:])
    rpc.result("eth_call", "0x" + "00" * 12 + OWNER[2:])
    rpc.result("eth_call", RpcResponseError("admin has no owner"))
    rpc.result(
        "eth_getBlockByNumber",
        {"hash": BLOCK_HASH, "transactions": [{
            "hash": "0xfailed", "from": OWNER, "to": TOKEN,
            "input": "0x12345678",
        }]},
    )
    rpc.result("eth_getTransactionReceipt", {"status": "0x0"})

    facts = await PrivilegedCallCollector("ethereum", rpc, TOKEN).collect(
        100, 100, admin=ADMIN, owner=OWNER, decoded_events=[]
    )

    assert facts == []


@pytest.mark.asyncio
async def test_proxy_admin_owner_is_treated_as_privileged() -> None:
    proxy_admin_owner = "0x" + "44" * 20
    rpc = FakeRpc()
    rpc.result("eth_getStorageAt", "0x" + "00" * 12 + ADMIN[2:])
    rpc.result("eth_call", "0x" + "00" * 12 + OWNER[2:])
    rpc.result("eth_call", "0x" + "00" * 12 + proxy_admin_owner[2:])
    rpc.result(
        "eth_getBlockByNumber",
        {"hash": BLOCK_HASH, "transactions": [{
            "hash": "0xupgrade", "from": proxy_admin_owner, "to": ADMIN,
            "input": "0x99a88ec4",
        }]},
    )
    rpc.result("eth_getTransactionReceipt", {"status": "0x1"})

    facts = await PrivilegedCallCollector("ethereum", rpc, TOKEN).collect(
        100, 100, admin=ADMIN, owner=OWNER, decoded_events=[]
    )

    assert len(facts) == 1
    assert facts[0].event_type == "PRIVILEGED_PROXY_ADMIN_UPGRADE"


@pytest.mark.asyncio
async def test_privileged_scan_includes_intermediate_admin() -> None:
    start_admin = "0x" + "77" * 20
    intermediate_admin = "0x" + "66" * 20
    rpc = FakeRpc()
    rpc.result("eth_getStorageAt", "0x" + "00" * 12 + start_admin[2:])
    rpc.result(
        "eth_getStorageAt", "0x" + "00" * 12 + intermediate_admin[2:]
    )
    rpc.result("eth_getStorageAt", "0x" + "00" * 12 + ADMIN[2:])
    for _ in range(6):
        rpc.result("eth_call", RpcResponseError("owner unsupported"))
    rpc.result("eth_getBlockByNumber", {"hash": BLOCK_HASH, "transactions": []})
    rpc.result(
        "eth_getBlockByNumber",
        {"hash": BLOCK_HASH, "transactions": [{
            "hash": "0xintermediate", "from": intermediate_admin, "to": TOKEN,
            "input": "0x8456cb59",
        }]},
    )
    rpc.result("eth_getBlockByNumber", {"hash": BLOCK_HASH, "transactions": []})
    rpc.result("eth_getTransactionReceipt", {"status": "0x1"})

    facts = await PrivilegedCallCollector("ethereum", rpc, TOKEN).collect(
        100, 102, admin=ADMIN, owner=OWNER, decoded_events=[]
    )

    assert len(facts) == 1
    assert facts[0].event_type == "PRIVILEGED_PAUSE"


@pytest.mark.asyncio
async def test_privileged_scan_tracks_owner_changes_within_block() -> None:
    owner_a = "0x" + "77" * 20
    owner_b = "0x" + "66" * 20
    owner_c = "0x" + "55" * 20
    rpc = FakeRpc()
    rpc.result("eth_getBlockByNumber", {"hash": BLOCK_HASH, "transactions": [
        {
            "hash": "0xpause", "from": owner_a, "to": TOKEN,
            "input": "0x8456cb59",
        },
        {
            "hash": "0xgrant", "from": owner_a, "to": TOKEN,
            "input": "0xf2fde38b" + "00" * 12 + owner_b[2:],
        },
        {
            "hash": "0xstale", "from": owner_a, "to": TOKEN,
            "input": "0x12345678",
        },
        {
            "hash": "0xreturn", "from": owner_b, "to": TOKEN,
            "input": "0xf2fde38b" + "00" * 12 + owner_c[2:],
        },
    ]})
    rpc.result("eth_getStorageAt", "0x" + "00" * 12 + ADMIN[2:])
    rpc.result("eth_call", "0x" + "00" * 12 + owner_a[2:])
    rpc.result("eth_call", RpcResponseError("admin has no owner"))
    for _ in range(3):
        rpc.result("eth_getTransactionReceipt", {"status": "0x1"})

    facts = await PrivilegedCallCollector("ethereum", rpc, TOKEN).collect(
        100, 100, admin=ADMIN, owner=owner_c, decoded_events=[]
    )

    assert [fact.tx_hash for fact in facts] == ["0xpause", "0xgrant", "0xreturn"]
    assert rpc.calls_for("eth_getStorageAt")[0][2] == "0x63"
    assert all(params[1] == "0x63" for params in rpc.calls_for("eth_call"))


@pytest.mark.asyncio
async def test_safe_execution_with_third_party_transfer_is_flagged() -> None:
    safe = "0x" + "44" * 20
    signer = "0x" + "55" * 20
    rpc = FakeRpc()
    rpc.result("eth_getStorageAt", "0x" + "00" * 12 + ADMIN[2:])
    rpc.result("eth_call", "0x" + "00" * 12 + safe[2:])
    rpc.result("eth_call", RpcResponseError("admin has no owner"))
    rpc.result(
        "eth_getBlockByNumber",
        {"hash": BLOCK_HASH, "transactions": [{
            "hash": "0xsafe", "from": signer, "to": safe,
            "input": safe_exec_input(TOKEN, "0x12345678deadbeef"),
        }]},
    )
    rpc.result("eth_getCode", "0x6000")
    rpc.result(
        "eth_getTransactionReceipt",
        {"status": "0x1", "logs": [{"topics": [EXECUTION_SUCCESS_TOPIC]}]},
    )
    transfer = DecodedEvent(
        chain="ethereum",
        event_type="TRANSFER",
        tx_hash="0xsafe",
        log_index=1,
        block_number=100,
        from_address="0x" + "11" * 20,
        to_address="0x" + "22" * 20,
        amount=1,
    )

    facts = await PrivilegedCallCollector("ethereum", rpc, TOKEN).collect(
        100, 100, admin=ADMIN, owner=OWNER, decoded_events=[transfer]
    )

    assert len(facts) == 1
    assert facts[0].event_type == "PRIVILEGED_UNKNOWN_CALL"
    assert facts[0].metadata["third_party_asset_move"] is True


@pytest.mark.asyncio
async def test_safe_execution_moving_safe_balance_is_not_third_party() -> None:
    safe = "0x" + "44" * 20
    signer = "0x" + "55" * 20
    rpc = FakeRpc()
    rpc.result("eth_getStorageAt", "0x" + "00" * 12 + ADMIN[2:])
    rpc.result("eth_call", "0x" + "00" * 12 + safe[2:])
    rpc.result("eth_call", RpcResponseError("admin has no owner"))
    rpc.result(
        "eth_getBlockByNumber",
        {"hash": BLOCK_HASH, "transactions": [{
            "hash": "0xsafe", "from": signer, "to": safe,
            "input": safe_exec_input(TOKEN, "0x12345678deadbeef"),
        }]},
    )
    rpc.result("eth_getCode", "0x6000")
    rpc.result(
        "eth_getTransactionReceipt",
        {"status": "0x1", "logs": [{"topics": [EXECUTION_SUCCESS_TOPIC]}]},
    )
    transfer = DecodedEvent(
        chain="ethereum",
        event_type="TRANSFER",
        tx_hash="0xsafe",
        log_index=1,
        block_number=100,
        from_address=safe,
        to_address="0x" + "22" * 20,
        amount=1,
    )

    facts = await PrivilegedCallCollector("ethereum", rpc, TOKEN).collect(
        100, 100, admin=ADMIN, owner=OWNER, decoded_events=[transfer]
    )

    assert len(facts) == 1
    assert facts[0].metadata["third_party_asset_move"] is False


@pytest.mark.asyncio
async def test_plain_successful_call_to_safe_owner_is_not_flagged() -> None:
    safe = "0x" + "44" * 20
    signer = "0x" + "55" * 20
    rpc = FakeRpc()
    rpc.result("eth_getStorageAt", "0x" + "00" * 12 + ADMIN[2:])
    rpc.result("eth_call", "0x" + "00" * 12 + safe[2:])
    rpc.result("eth_call", RpcResponseError("admin has no owner"))
    rpc.result(
        "eth_getBlockByNumber",
        {"hash": BLOCK_HASH, "transactions": [{
            "hash": "0xsafe", "from": signer, "to": safe,
            "input": "0x12345678",
        }]},
    )
    rpc.result("eth_getCode", "0x6000")

    facts = await PrivilegedCallCollector("ethereum", rpc, TOKEN).collect(
        100, 100, admin=ADMIN, owner=OWNER, decoded_events=[]
    )

    assert facts == []
    assert rpc.calls_for("eth_getTransactionReceipt") == []


@pytest.mark.asyncio
async def test_failed_safe_inner_execution_is_not_flagged() -> None:
    safe = "0x" + "44" * 20
    signer = "0x" + "55" * 20
    rpc = FakeRpc()
    rpc.result("eth_getStorageAt", encoded_address(ADMIN))
    rpc.result("eth_call", encoded_address(safe))
    rpc.result("eth_call", RpcResponseError("admin has no owner"))
    rpc.result(
        "eth_getBlockByNumber",
        {"hash": BLOCK_HASH, "transactions": [{
            "hash": "0xsafe", "from": signer, "to": safe,
            "input": safe_exec_input(TOKEN, "0x8456cb59"),
        }]},
    )
    rpc.result("eth_getCode", "0x6000")
    rpc.result(
        "eth_getTransactionReceipt",
        {"status": "0x1", "logs": [{"topics": [EXECUTION_FAILURE_TOPIC]}]},
    )

    facts = await PrivilegedCallCollector("ethereum", rpc, TOKEN).collect(
        100, 100, admin=ADMIN, owner=OWNER, decoded_events=[]
    )

    assert facts == []


@pytest.mark.asyncio
async def test_unprivileged_call_to_privileged_eoa_is_not_flagged() -> None:
    attacker = "0x" + "55" * 20
    rpc = FakeRpc()
    rpc.result("eth_getStorageAt", "0x" + "00" * 12 + ADMIN[2:])
    rpc.result("eth_call", "0x" + "00" * 12 + OWNER[2:])
    rpc.result("eth_call", RpcResponseError("admin has no owner"))
    rpc.result(
        "eth_getBlockByNumber",
        {"hash": BLOCK_HASH, "transactions": [{
            "hash": "0xnoise", "from": attacker, "to": OWNER,
            "input": "0x6a761202",
        }]},
    )
    rpc.result("eth_getCode", "0x")

    facts = await PrivilegedCallCollector("ethereum", rpc, TOKEN).collect(
        100, 100, admin=ADMIN, owner=OWNER, decoded_events=[]
    )

    assert facts == []
    assert rpc.calls_for("eth_getTransactionReceipt") == []


@pytest.mark.asyncio
async def test_unprivileged_known_call_to_admin_eoa_is_not_flagged() -> None:
    attacker = "0x" + "55" * 20
    rpc = FakeRpc()
    rpc.result("eth_getStorageAt", "0x" + "00" * 12 + ADMIN[2:])
    rpc.result("eth_call", "0x" + "00" * 12 + OWNER[2:])
    rpc.result("eth_call", RpcResponseError("admin has no owner"))
    rpc.result(
        "eth_getBlockByNumber",
        {"hash": BLOCK_HASH, "transactions": [{
            "hash": "0xnoise", "from": attacker, "to": ADMIN,
            "input": "0x99a88ec4",
        }]},
    )
    rpc.result("eth_getCode", "0x")

    facts = await PrivilegedCallCollector("ethereum", rpc, TOKEN).collect(
        100, 100, admin=ADMIN, owner=OWNER, decoded_events=[]
    )

    assert facts == []
    assert rpc.calls_for("eth_getTransactionReceipt") == []


@pytest.mark.asyncio
async def test_privileged_block_reads_use_bounded_concurrency() -> None:
    class ConcurrentRpc:
        def __init__(self) -> None:
            self.in_flight = 0
            self.max_in_flight = 0

        async def call(self, method, params):
            if method == "eth_call":
                raise RpcResponseError("admin has no owner")
            if method == "eth_getStorageAt":
                return "0x" + "00" * 12 + ADMIN[2:]
            assert method == "eth_getBlockByNumber"
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            await asyncio.sleep(0.01)
            self.in_flight -= 1
            return {"hash": BLOCK_HASH, "transactions": []}

    rpc = ConcurrentRpc()

    facts = await PrivilegedCallCollector("bsc", rpc, TOKEN).collect(
        100, 109, admin=ADMIN, owner=OWNER, decoded_events=[]
    )

    assert facts == []
    assert rpc.max_in_flight == 8


@pytest.mark.asyncio
async def test_privileged_block_task_count_is_bounded(monkeypatch) -> None:
    active = 0
    peak = 0
    original_create_task = asyncio.TaskGroup.create_task

    def tracked_create_task(self, coro, *args, **kwargs):
        nonlocal active, peak
        task = original_create_task(self, coro, *args, **kwargs)
        active += 1
        peak = max(peak, active)

        def finished(_task):
            nonlocal active
            active -= 1

        task.add_done_callback(finished)
        return task

    monkeypatch.setattr(asyncio.TaskGroup, "create_task", tracked_create_task)

    class Rpc:
        async def call(self, method, params):
            if method == "eth_call":
                raise RpcResponseError("admin has no owner")
            if method == "eth_getStorageAt":
                return "0x" + "00" * 12 + ADMIN[2:]
            await asyncio.sleep(0)
            return {"hash": BLOCK_HASH, "transactions": []}

    await PrivilegedCallCollector(
        "bsc", Rpc(), TOKEN, max_concurrency=2, block_batch_size=3
    ).collect(100, 109, admin=ADMIN, owner=OWNER, decoded_events=[])

    assert peak <= 3
