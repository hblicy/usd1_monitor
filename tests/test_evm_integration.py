from datetime import UTC, datetime

import pytest

from tests.fakes import FakeRpc
from usd1_monitor.collectors.evm import EvmScanner, EvmSnapshot, ScanResult
from usd1_monitor.models import ChainEvent, Observation, RiskLevel
from usd1_monitor.scheduler import (
    CheckResult,
    DeliveryRateLimiter,
    EvmChainMonitor,
    Usd1Monitor,
)


NOW = datetime(2026, 9, 7, 4, 0, tzinfo=UTC)
ADDRESS_A = "0x" + "11" * 20
ADDRESS_B = "0x" + "22" * 20
ADMIN = "0x" + "33" * 20


def snapshot(
    chain: str,
    block: int,
    implementation: str,
    *,
    paused: bool | None = False,
    frozen_accounts: dict[str, bool] | None = None,
    code_hash: str = "0xhash",
) -> EvmSnapshot:
    values = (
        Observation("evm.implementation", "evm_rpc", chain, 1, "address", NOW, NOW, metadata={"address": implementation, "block": block}),
        Observation("evm.admin", "evm_rpc", chain, 1, "address", NOW, NOW, metadata={"address": ADMIN, "block": block}),
        Observation("evm.code_hash", "evm_rpc", chain, 1, "hash", NOW, NOW, metadata={"hash": code_hash, "block": block}),
        Observation("evm.owner", "evm_rpc", chain, 0, "address", NOW, NOW, metadata={"address": None, "supported": False, "block": block}),
        Observation(
            "evm.paused",
            "evm_rpc",
            chain,
            float(paused or False),
            "bool",
            NOW,
            NOW,
            metadata={"supported": paused is not None, "block": block},
        ),
    )
    return EvmSnapshot(
        chain,
        block,
        implementation,
        ADMIN,
        code_hash,
        None,
        paused,
        values,
        frozen_accounts=frozen_accounts or {},
    )


class FakeScanner:
    def __init__(self, results) -> None:
        self.results = list(results)

    async def scan_once(self):
        value = self.results.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class FakeSnapshotReader:
    def __init__(self, values) -> None:
        self.values = list(values)

    async def read(self, safe_block, collected_at):
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def scan(chain: str, block: int, events=()) -> ScanResult:
    return ScanResult(block, block, block, len(events), block, tuple(events))


@pytest.mark.asyncio
async def test_implementation_upgrade_alerts_once_and_replay_is_quiet(
    storage, fake_notifier
) -> None:
    reader = FakeSnapshotReader(
        [
            snapshot("ethereum", 100, ADDRESS_A),
            snapshot("ethereum", 101, ADDRESS_B),
            snapshot("ethereum", 102, ADDRESS_B),
        ]
    )
    monitor = EvmChainMonitor(
        "ethereum",
        FakeScanner([scan("ethereum", 100), scan("ethereum", 101), scan("ethereum", 102)]),
        reader,
        storage,
        notifier=fake_notifier,
    )

    await monitor.check_once()
    await monitor.check_once()
    await monitor.check_once()

    assert len(fake_notifier.messages) == 1
    assert "RED" in fake_notifier.messages[0]
    states = await storage.list_risk_states()
    assert any(item.level is RiskLevel.RED for item in states)


@pytest.mark.asyncio
async def test_cold_start_detects_current_paused_state(storage, fake_notifier) -> None:
    monitor = EvmChainMonitor(
        "ethereum",
        FakeScanner([scan("ethereum", 100)]),
        FakeSnapshotReader([snapshot("ethereum", 100, ADDRESS_A, paused=True)]),
        storage,
        notifier=fake_notifier,
    )

    result = await monitor.check_once()

    assert result.success is True
    state = await storage.get_risk_state("evm.ethereum.paused")
    assert state is not None and state.level is RiskLevel.RED
    assert len(fake_notifier.messages) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current_paused", "expected_level"),
    [(True, RiskLevel.RED), (False, RiskLevel.GREEN)],
)
async def test_paused_becoming_supported_establishes_current_state(
    storage,
    current_paused: bool,
    expected_level: RiskLevel,
) -> None:
    monitor = EvmChainMonitor(
        "ethereum",
        FakeScanner([scan("ethereum", 100), scan("ethereum", 101)]),
        FakeSnapshotReader(
            [
                snapshot("ethereum", 100, ADDRESS_A, paused=None),
                snapshot("ethereum", 101, ADDRESS_A, paused=current_paused),
            ]
        ),
        storage,
    )

    await monitor.check_once(deliver=False)
    await monitor.check_once(deliver=False)

    state = await storage.get_risk_state("evm.ethereum.paused")
    assert state is not None and state.level is expected_level


@pytest.mark.asyncio
async def test_cold_start_detects_current_watched_frozen_state(
    storage, fake_notifier
) -> None:
    monitor = EvmChainMonitor(
        "ethereum",
        FakeScanner([scan("ethereum", 100)]),
        FakeSnapshotReader(
            [
                snapshot(
                    "ethereum",
                    100,
                    ADDRESS_A,
                    frozen_accounts={ADDRESS_B: True},
                )
            ]
        ),
        storage,
        watched_addresses={ADDRESS_B},
        notifier=fake_notifier,
    )

    result = await monitor.check_once()

    assert result.success is True
    state = await storage.get_risk_state(f"evm.ethereum.freeze.{ADDRESS_B}")
    assert state is not None and state.level is RiskLevel.RED
    assert len(fake_notifier.messages) == 1


@pytest.mark.asyncio
async def test_cold_start_unfrozen_watch_does_not_send_recovery(
    storage, fake_notifier
) -> None:
    monitor = EvmChainMonitor(
        "ethereum",
        FakeScanner([scan("ethereum", 100)]),
        FakeSnapshotReader(
            [
                snapshot(
                    "ethereum",
                    100,
                    ADDRESS_A,
                    frozen_accounts={ADDRESS_B: False},
                )
            ]
        ),
        storage,
        watched_addresses={ADDRESS_B},
        notifier=fake_notifier,
    )

    await monitor.check_once()

    state = await storage.get_risk_state(f"evm.ethereum.freeze.{ADDRESS_B}")
    assert state is not None and state.level is RiskLevel.GREEN
    assert fake_notifier.messages == []


@pytest.mark.asyncio
async def test_monitor_commits_only_processed_catch_up_cursor(storage) -> None:
    class CapturingReader:
        safe_block = None

        async def read(self, safe_block, collected_at):
            self.safe_block = safe_block
            return snapshot("ethereum", safe_block, ADDRESS_A)

    reader = CapturingReader()
    monitor = EvmChainMonitor(
        "ethereum",
        FakeScanner([ScanResult(10_000, 9_997, 180, 0, 81, ())]),
        reader,
        storage,
    )

    result = await monitor.check_once(deliver=False)

    assert result.success is True
    assert reader.safe_block == 180
    assert await storage.get_scan_cursor("ethereum") == 180


@pytest.mark.asyncio
async def test_unfreeze_sends_recovery_for_same_account(storage, fake_notifier) -> None:
    freeze = ChainEvent("bsc", 100, "0xfreeze", 1, "FREEZE", {"account": ADDRESS_A}, NOW)
    unfreeze = ChainEvent("bsc", 101, "0xunfreeze", 1, "UNFREEZE", {"account": ADDRESS_A}, NOW)
    monitor = EvmChainMonitor(
        "bsc",
        FakeScanner([scan("bsc", 100, [freeze]), scan("bsc", 101, [unfreeze])]),
        FakeSnapshotReader([snapshot("bsc", 100, ADDRESS_A), snapshot("bsc", 101, ADDRESS_A)]),
        storage,
        watched_addresses={ADDRESS_A},
        notifier=fake_notifier,
    )

    await monitor.check_once()
    await monitor.check_once()

    assert len(fake_notifier.messages) == 2
    assert "恢复" in fake_notifier.messages[1]


@pytest.mark.asyncio
async def test_known_proxy_admin_upgrade_enters_risk_engine(
    storage, fake_notifier
) -> None:
    upgrade = ChainEvent(
        "ethereum",
        101,
        "0xupgrade",
        -1,
        "PRIVILEGED_PROXY_ADMIN_UPGRADE",
        {"selector": "0x99a88ec4"},
        NOW,
    )
    monitor = EvmChainMonitor(
        "ethereum",
        FakeScanner([scan("ethereum", 101, [upgrade])]),
        FakeSnapshotReader([snapshot("ethereum", 101, ADDRESS_A)]),
        storage,
        notifier=fake_notifier,
    )

    await monitor.check_once()

    states = await storage.list_risk_states()
    state = next(item for item in states if "0xupgrade:-1" in item.rule_id)
    assert state.level is RiskLevel.RED
    assert len(fake_notifier.messages) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["MINT", "BURN"])
async def test_equivalent_transfer_and_custom_supply_events_alert_once(
    storage, event_type: str
) -> None:
    zero = "0x" + "00" * 20
    transfer_payload = {
        "account": None,
        "from_address": zero if event_type == "MINT" else ADDRESS_A,
        "to_address": ADDRESS_A if event_type == "MINT" else zero,
        "amount": 10_000_000,
    }
    custom_payload = {
        "account": ADDRESS_A,
        "from_address": None,
        "to_address": None,
        "amount": 10_000_000,
    }
    events = (
        ChainEvent(
            "ethereum", 100, "0xsupply", 1, event_type, transfer_payload, NOW
        ),
        ChainEvent(
            "ethereum", 100, "0xsupply", 2, event_type, custom_payload, NOW
        ),
    )
    monitor = EvmChainMonitor(
        "ethereum",
        FakeScanner([scan("ethereum", 100, events)]),
        FakeSnapshotReader([snapshot("ethereum", 100, ADDRESS_A)]),
        storage,
    )

    await monitor.check_once(deliver=False)

    assert await storage.count_alert_deliveries() == 1
    states = [
        item
        for item in await storage.list_risk_states()
        if item.rule_id.startswith("event.evm.ethereum.0xsupply")
    ]
    assert len(states) == 1


@pytest.mark.asyncio
async def test_two_distinct_transfer_mints_with_same_values_are_preserved(
    storage,
) -> None:
    zero = "0x" + "00" * 20
    payload = {
        "account": None,
        "from_address": zero,
        "to_address": ADDRESS_A,
        "amount": 10_000_000,
    }
    events = (
        ChainEvent("ethereum", 100, "0xsupply", 1, "MINT", payload, NOW),
        ChainEvent("ethereum", 100, "0xsupply", 2, "MINT", payload, NOW),
    )
    monitor = EvmChainMonitor(
        "ethereum",
        FakeScanner([scan("ethereum", 100, events)]),
        FakeSnapshotReader([snapshot("ethereum", 100, ADDRESS_A)]),
        storage,
    )

    await monitor.check_once(deliver=False)

    assert await storage.count_alert_deliveries() == 2


@pytest.mark.asyncio
async def test_two_transfer_and_custom_mint_pairs_alert_twice(storage) -> None:
    zero = "0x" + "00" * 20
    transfer_payload = {
        "account": None,
        "from_address": zero,
        "to_address": ADDRESS_A,
        "amount": 10_000_000,
    }
    custom_payload = {
        "account": ADDRESS_A,
        "from_address": None,
        "to_address": None,
        "amount": 10_000_000,
    }
    events = tuple(
        ChainEvent(
            "ethereum",
            100,
            "0xsupply",
            log_index,
            "MINT",
            transfer_payload if log_index % 2 else custom_payload,
            NOW,
        )
        for log_index in range(1, 5)
    )
    monitor = EvmChainMonitor(
        "ethereum",
        FakeScanner([scan("ethereum", 100, events)]),
        FakeSnapshotReader([snapshot("ethereum", 100, ADDRESS_A)]),
        storage,
    )

    await monitor.check_once(deliver=False)

    assert await storage.count_alert_deliveries() == 2


@pytest.mark.asyncio
async def test_upgrade_event_and_snapshot_changes_share_one_alert(
    storage, fake_notifier
) -> None:
    upgrade = ChainEvent(
        "ethereum",
        101,
        "0xupgrade",
        -1,
        "PRIVILEGED_PROXY_ADMIN_UPGRADE",
        {"selector": "0x99a88ec4"},
        NOW,
    )
    monitor = EvmChainMonitor(
        "ethereum",
        FakeScanner([scan("ethereum", 100), scan("ethereum", 101, [upgrade])]),
        FakeSnapshotReader(
            [
                snapshot("ethereum", 100, ADDRESS_A, code_hash="0xhash-a"),
                snapshot("ethereum", 101, ADDRESS_B, code_hash="0xhash-b"),
            ]
        ),
        storage,
        notifier=fake_notifier,
    )

    await monitor.check_once()
    await monitor.check_once()

    assert len(fake_notifier.messages) == 1
    assert "IMPLEMENTATION_CHANGED" in fake_notifier.messages[0]
    assert "CODE_HASH_CHANGED" in fake_notifier.messages[0]


@pytest.mark.asyncio
async def test_overlap_reconciles_event_removed_by_reorg(
    storage, fake_notifier
) -> None:
    freeze = ChainEvent(
        "bsc", 100, "0xorphan", 1, "FREEZE", {"account": ADDRESS_A}, NOW
    )
    scans = [
        ScanResult(100, 100, 100, 1, 100, (freeze,)),
        ScanResult(101, 101, 101, 0, 100, ()),
    ]
    monitor = EvmChainMonitor(
        "bsc",
        FakeScanner(scans),
        FakeSnapshotReader(
            [snapshot("bsc", 100, ADDRESS_A), snapshot("bsc", 101, ADDRESS_A)]
        ),
        storage,
        watched_addresses={ADDRESS_A},
        notifier=fake_notifier,
    )

    assert (await monitor.check_once()).success is True
    assert (await storage.get_risk_state(
        f"evm.bsc.freeze.{ADDRESS_A.lower()}"
    )).level is RiskLevel.RED

    assert (await monitor.check_once()).success is True

    assert await storage.count_chain_events() == 0
    state = await storage.get_risk_state(f"evm.bsc.freeze.{ADDRESS_A.lower()}")
    assert state is not None and state.level is RiskLevel.GREEN
    assert any("reorg" in message.lower() for message in fake_notifier.messages)
    assert any(
        "https://bscscan.com/block/101" in message
        for message in fake_notifier.messages
    )


@pytest.mark.asyncio
async def test_reorg_keeps_current_watched_frozen_snapshot_red(storage) -> None:
    orphan_unfreeze = ChainEvent(
        "bsc", 100, "0xorphan", 1, "UNFREEZE", {"account": ADDRESS_A}, NOW
    )
    monitor = EvmChainMonitor(
        "bsc",
        FakeScanner(
            [
                ScanResult(99, 99, 99, 0, 99, ()),
                ScanResult(100, 100, 100, 1, 100, (orphan_unfreeze,)),
                ScanResult(101, 101, 101, 0, 100, ()),
            ]
        ),
        FakeSnapshotReader(
            [
                snapshot(
                    "bsc", 99, ADDRESS_A, frozen_accounts={ADDRESS_A: True}
                ),
                snapshot(
                    "bsc", 100, ADDRESS_A, frozen_accounts={ADDRESS_A: False}
                ),
                snapshot(
                    "bsc", 101, ADDRESS_A, frozen_accounts={ADDRESS_A: True}
                ),
            ]
        ),
        storage,
        watched_addresses={ADDRESS_A},
    )

    await monitor.check_once(deliver=False)
    await monitor.check_once(deliver=False)
    await monitor.check_once(deliver=False)

    state = await storage.get_risk_state(f"evm.bsc.freeze.{ADDRESS_A}")
    assert state is not None and state.level is RiskLevel.RED


@pytest.mark.asyncio
async def test_block_hash_reorg_removes_orphan_snapshot_state(storage) -> None:
    class HashedPrivilegedCollector:
        def __init__(self):
            self.hashes = [
                {99: "0x99a"},
                {100: "0x100a"},
                {100: "0x100b", 101: "0x101b"},
            ]
            self.last_block_hashes = {}

        async def collect(self, *args, **kwargs):
            self.last_block_hashes = self.hashes.pop(0)
            return []

    monitor = EvmChainMonitor(
        "ethereum",
        FakeScanner([
            ScanResult(99, 99, 99, 0, 99, ()),
            ScanResult(100, 100, 100, 0, 100, ()),
            ScanResult(101, 101, 101, 0, 100, ()),
        ]),
        FakeSnapshotReader([
            snapshot("ethereum", 99, ADDRESS_A),
            snapshot("ethereum", 100, ADDRESS_B),
            snapshot("ethereum", 101, ADDRESS_A),
        ]),
        storage,
        privileged_collector=HashedPrivilegedCollector(),
    )
    await monitor.check_once(deliver=False)
    await monitor.check_once(deliver=False)
    orphan_rule = "evm.event.ethereum.snapshot:100:evm.implementation"
    assert await storage.get_risk_state(orphan_rule) is not None
    assert any(
        alert.alert_key.startswith(
            "snapshot:ethereum:100:evm.implementation:"
        )
        for alert in await storage.pending_alerts()
    )

    await monitor.check_once(deliver=False)

    assert await storage.get_risk_state(orphan_rule) is None


@pytest.mark.asyncio
async def test_snapshot_only_reorg_cancels_orphan_frozen_alert(storage) -> None:
    class HashedPrivilegedCollector:
        def __init__(self) -> None:
            self.hashes = [
                {100: "0x100a"},
                {100: "0x100b", 101: "0x101b"},
            ]
            self.last_block_hashes = {}

        async def collect(self, *args, **kwargs):
            self.last_block_hashes = self.hashes.pop(0)
            return []

    monitor = EvmChainMonitor(
        "bsc",
        FakeScanner([
            ScanResult(100, 100, 100, 0, 100, ()),
            ScanResult(101, 101, 101, 0, 100, ()),
        ]),
        FakeSnapshotReader([
            snapshot(
                "bsc", 100, ADDRESS_A,
                frozen_accounts={ADDRESS_A: True},
            ),
            snapshot(
                "bsc", 101, ADDRESS_A,
                frozen_accounts={ADDRESS_A: False},
            ),
        ]),
        storage,
        watched_addresses={ADDRESS_A},
        privileged_collector=HashedPrivilegedCollector(),
    )
    other_chain_key = "snapshot:ethereum:100:paused:other-chain"
    async with storage.write_lock:
        await storage.insert_pending_alert_uncommitted(
            other_chain_key,
            "hash",
            "chain=ethereum fact_type=PAUSED",
            NOW,
        )
        await storage.connection.commit()

    await monitor.check_once(deliver=False)
    stale_keys = {
        item.alert_key
        for item in await storage.pending_alerts()
        if ":frozen:" in item.alert_key
    }
    assert stale_keys

    await monitor.check_once(deliver=False)

    remaining_keys = {item.alert_key for item in await storage.pending_alerts()}
    assert stale_keys.isdisjoint(remaining_keys)
    assert other_chain_key in remaining_keys


@pytest.mark.asyncio
async def test_snapshot_only_reorg_restores_paused_state_from_current_snapshot(
    storage,
) -> None:
    class HashedPrivilegedCollector:
        def __init__(self) -> None:
            self.hashes = [
                {99: "0x99a"},
                {100: "0x100a"},
                {100: "0x100b", 101: "0x101b"},
            ]
            self.last_block_hashes = {}

        async def collect(self, *args, **kwargs):
            self.last_block_hashes = self.hashes.pop(0)
            return []

    monitor = EvmChainMonitor(
        "ethereum",
        FakeScanner([
            ScanResult(99, 99, 99, 0, 99, ()),
            ScanResult(100, 100, 100, 0, 100, ()),
            ScanResult(101, 101, 101, 0, 100, ()),
        ]),
        FakeSnapshotReader([
            snapshot("ethereum", 99, ADDRESS_A, paused=False),
            snapshot("ethereum", 100, ADDRESS_A, paused=True),
            snapshot("ethereum", 101, ADDRESS_A, paused=False),
        ]),
        storage,
        privileged_collector=HashedPrivilegedCollector(),
    )

    await monitor.check_once(deliver=False)
    await monitor.check_once(deliver=False)
    state = await storage.get_risk_state("evm.ethereum.paused")
    assert state is not None and state.level is RiskLevel.RED

    await monitor.check_once(deliver=False)

    state = await storage.get_risk_state("evm.ethereum.paused")
    assert state is not None and state.level is RiskLevel.GREEN


@pytest.mark.asyncio
async def test_second_reorg_cancels_previous_snapshot_correction(storage) -> None:
    class HashedPrivilegedCollector:
        def __init__(self) -> None:
            self.hashes = [
                {99: "0x99a"},
                {100: "0x100a"},
                {100: "0x100b", 101: "0x101b"},
                {101: "0x101c", 102: "0x102c"},
            ]
            self.last_block_hashes = {}

        async def collect(self, *args, **kwargs):
            self.last_block_hashes = self.hashes.pop(0)
            return []

    monitor = EvmChainMonitor(
        "ethereum",
        FakeScanner([
            ScanResult(99, 99, 99, 0, 99, ()),
            ScanResult(100, 100, 100, 0, 100, ()),
            ScanResult(101, 101, 101, 0, 100, ()),
            ScanResult(102, 102, 102, 0, 101, ()),
        ]),
        FakeSnapshotReader([
            snapshot("ethereum", 99, ADDRESS_A, paused=False),
            snapshot("ethereum", 100, ADDRESS_A, paused=True),
            snapshot("ethereum", 101, ADDRESS_A, paused=False),
            snapshot("ethereum", 102, ADDRESS_A, paused=True),
        ]),
        storage,
        privileged_collector=HashedPrivilegedCollector(),
    )

    await monitor.check_once(deliver=False)
    await monitor.check_once(deliver=False)
    await monitor.check_once(deliver=False)
    first_correction = {
        item.alert_key
        for item in await storage.pending_alerts()
        if item.alert_key.startswith("reorg-snapshot:")
    }
    assert first_correction

    await monitor.check_once(deliver=False)

    remaining = {item.alert_key for item in await storage.pending_alerts()}
    assert first_correction.isdisjoint(remaining)
    assert any(
        key.startswith("snapshot:ethereum:102:paused:")
        for key in remaining
    )
    state = await storage.get_risk_state("evm.ethereum.paused")
    assert state is not None and state.level is RiskLevel.RED


class FakeMarketMonitor:
    async def check_once(self, *, deliver=True):
        return CheckResult(True, ())


@pytest.mark.asyncio
async def test_ethereum_failure_does_not_block_bsc_event(
    storage, fake_notifier
) -> None:
    ethereum = EvmChainMonitor(
        "ethereum",
        FakeScanner([RuntimeError("rpc offline")]),
        FakeSnapshotReader([]),
        storage,
    )
    freeze = ChainEvent("bsc", 100, "0xfreeze", 1, "FREEZE", {"account": ADDRESS_A}, NOW)
    bsc = EvmChainMonitor(
        "bsc",
        FakeScanner([scan("bsc", 100, [freeze])]),
        FakeSnapshotReader([snapshot("bsc", 100, ADDRESS_A)]),
        storage,
        watched_addresses={ADDRESS_A},
    )
    monitor = Usd1Monitor(
        FakeMarketMonitor(), [ethereum, bsc], storage, fake_notifier
    )

    result = await monitor.check_once()

    assert result.success is False
    assert await storage.get_risk_state(
        f"evm.bsc.freeze.{ADDRESS_A.lower()}"
    ) is not None
    assert len(fake_notifier.messages) == 1


@pytest.mark.asyncio
async def test_downstream_failure_does_not_advance_evm_cursor(storage) -> None:
    rpc = FakeRpc()
    rpc.result("eth_blockNumber", "0x20")
    rpc.result(
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
    monitor = EvmChainMonitor(
        "ethereum",
        EvmScanner(
            "ethereum",
            rpc,
            storage,
            confirmation_depth=3,
            overlap_blocks=20,
            batch_blocks=100,
        ),
        FakeSnapshotReader([RuntimeError("snapshot failed")]),
        storage,
    )

    result = await monitor.check_once()

    assert result.success is False
    assert await storage.get_scan_cursor("ethereum") is None
    assert await storage.count_chain_events() == 0


@pytest.mark.asyncio
async def test_composite_monitor_prunes_expired_observations(storage) -> None:
    old = datetime(2020, 1, 1, tzinfo=UTC)
    await storage.insert_observation(
        Observation(
            "market.mid_price", "binance", "USD1USDT", 1.0,
            "USDT", old, old,
        )
    )
    monitor = Usd1Monitor(
        FakeMarketMonitor(), [], storage, None, retention_days=180
    )

    await monitor.check_once(deliver=False)

    assert await storage.latest_observations("market.mid_price", limit=1) == []


@pytest.mark.asyncio
async def test_composite_monitor_recovers_expired_event_state(storage) -> None:
    old = datetime.now(UTC) - __import__("datetime").timedelta(hours=2)
    rule_id = "event.evm.ethereum.0xold:0"
    await storage.set_risk_state(rule_id, RiskLevel.YELLOW, old, old)
    monitor = Usd1Monitor(FakeMarketMonitor(), [], storage, None)

    await monitor.check_once(deliver=False)

    state = await storage.get_risk_state(rule_id)
    assert state is not None and state.level is RiskLevel.GREEN


@pytest.mark.asyncio
async def test_information_event_expiry_is_silent_but_other_expiry_is_visible(
    storage,
) -> None:
    old = datetime.now(UTC) - __import__("datetime").timedelta(hours=2)
    information_rule = "event.information.binance.old-risk.hash"
    evm_rule = "event.evm.ethereum.0xold:0"
    await storage.set_risk_state(
        information_rule, RiskLevel.YELLOW, old, old
    )
    await storage.set_risk_state(evm_rule, RiskLevel.YELLOW, old, old)
    monitor = Usd1Monitor(FakeMarketMonitor(), [], storage, None)

    await monitor.check_once(deliver=False)

    information_state = await storage.get_risk_state(information_rule)
    evm_state = await storage.get_risk_state(evm_rule)
    assert information_state is not None
    assert information_state.level is RiskLevel.GREEN
    assert evm_state is not None
    assert evm_state.level is RiskLevel.GREEN
    pending = await storage.pending_alerts()
    assert len(pending) == 1
    assert pending[0].alert_key.startswith(f"expiry:{evm_rule}:")


@pytest.mark.asyncio
async def test_startup_notification_failure_does_not_stop_monitor(storage) -> None:
    class FailingNotifier:
        async def send_text(self, content: str) -> None:
            raise RuntimeError("webhook offline")

    monitor = Usd1Monitor(
        FakeMarketMonitor(), [], storage, FailingNotifier()
    )

    await monitor.send_startup_once()

    health = await storage.get_collector_health("notification_wechat")
    assert health is not None
    assert health.consecutive_failures == 1


@pytest.mark.asyncio
async def test_startup_notification_consumes_shared_delivery_rate_limit(storage) -> None:
    class RecordingNotifier:
        async def send_text(self, content: str) -> None:
            return None

    monitor = Usd1Monitor(
        FakeMarketMonitor(), [], storage, RecordingNotifier()
    )
    monitor._delivery_rate_limiter = DeliveryRateLimiter(
        storage,
        max_messages=1,
        window_seconds=60,
        clock=lambda: NOW,
    )

    await monitor.send_startup_once()

    assert await monitor._delivery_rate_limiter.try_acquire() is False


@pytest.mark.asyncio
async def test_composite_run_can_stop_gracefully(storage) -> None:
    class StoppingMarket:
        monitor = None

        async def check_once(self, *, deliver=True):
            self.monitor.stop()
            return CheckResult(True, ())

    market = StoppingMarket()
    monitor = Usd1Monitor(market, [], storage, None, interval_seconds=60)
    market.monitor = monitor

    await monitor.run()


@pytest.mark.asyncio
async def test_composite_check_does_not_let_slow_market_block_evm(storage) -> None:
    evm_started = __import__("asyncio").Event()

    class WaitingMarket:
        async def check_once(self, *, deliver=True):
            await evm_started.wait()
            return CheckResult(True, ())

    class QuickEvm:
        chain = "ethereum"

        async def check_once(self, *, deliver=True):
            evm_started.set()
            return CheckResult(True, ())

    monitor = Usd1Monitor(WaitingMarket(), [QuickEvm()], storage, None)

    result = await __import__("asyncio").wait_for(
        monitor.check_once(deliver=False), timeout=0.2
    )

    assert result.success is True


@pytest.mark.asyncio
async def test_run_schedules_components_independently(storage) -> None:
    market_cancelled = __import__("asyncio").Event()

    class StuckMarket:
        async def check_once(self, *, deliver=True):
            try:
                await __import__("asyncio").Event().wait()
            finally:
                market_cancelled.set()

    class StoppingEvm:
        chain = "ethereum"
        monitor = None

        async def check_once(self, *, deliver=True):
            self.monitor.stop()
            return CheckResult(True, ())

    evm = StoppingEvm()
    monitor = Usd1Monitor(
        StuckMarket(), [evm], storage, None,
        interval_seconds=60, check_timeout_seconds=5,
    )
    evm.monitor = monitor

    await __import__("asyncio").wait_for(monitor.run(), timeout=0.2)

    assert market_cancelled.is_set()


def test_component_loops_use_their_own_poll_intervals(storage) -> None:
    class Component:
        def __init__(self, interval: int) -> None:
            self.tick_interval_seconds = interval

        async def check_once(self, *, deliver=True):
            return CheckResult(True, ())

    reserve = Component(300)
    information = Component(900)
    monitor = Usd1Monitor(
        FakeMarketMonitor(),
        [],
        storage,
        None,
        interval_seconds=3600,
        reserve_supply=reserve,
        information=information,
    )

    intervals = {
        name: interval for name, _check, interval in monitor._component_checks()
    }

    assert intervals == {
        "market": 3600,
        "reserve_supply": 300,
        "information": 900,
    }


def test_each_evm_chain_uses_its_own_poll_interval(storage) -> None:
    class EvmComponent:
        def __init__(self, chain: str, interval: int) -> None:
            self.chain = chain
            self.interval_seconds = interval

        async def check_once(self, *, deliver=True):
            return CheckResult(True, ())

    monitor = Usd1Monitor(
        FakeMarketMonitor(),
        [EvmComponent("ethereum", 30), EvmComponent("bsc", 15)],
        storage,
        None,
        interval_seconds=3600,
    )

    intervals = {
        name: interval for name, _check, interval in monitor._component_checks()
    }

    assert intervals == {
        "market": 3600,
        "evm_ethereum": 30,
        "evm_bsc": 15,
    }


@pytest.mark.asyncio
async def test_composite_check_times_out_stuck_component(storage) -> None:
    class StuckMarket:
        async def check_once(self, *, deliver=True):
            await __import__("asyncio").Event().wait()

    monitor = Usd1Monitor(
        StuckMarket(), [], storage, None, check_timeout_seconds=0.01
    )

    result = await monitor.check_once(deliver=False)

    assert result.success is False
    assert "TimeoutError" in result.errors[0]
    health = await storage.get_collector_health("scheduler_market")
    assert health is not None and health.consecutive_failures == 1
