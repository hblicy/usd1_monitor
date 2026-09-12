from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from usd1_monitor.collectors.custody import (
    CustodyCollection,
    CustodyDataError,
    CustodyFailure,
)
from usd1_monitor.config import CustodyConfig
from usd1_monitor.models import ChainEvent, Observation, RiskLevel
from usd1_monitor.scheduler import (
    CheckResult,
    CustodyConcentrationMonitor,
    Usd1Monitor,
)


NOW = datetime(2026, 9, 11, 4, 0, tzinfo=UTC)
ETH = "0x" + "11" * 20
BSC = "0x" + "22" * 20
SOL = "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9"


def custody_config(*, verified_on: date = date(2026, 9, 11)) -> CustodyConfig:
    return CustodyConfig.model_validate(
        {
            "addresses": [
                {
                    "chain": "ethereum",
                    "address": ETH,
                    "entity": "binance_cex",
                    "label": "Binance ETH",
                    "role": "hot_wallet",
                    "status": "trusted",
                    "verified_on": verified_on.isoformat(),
                    "evidence": [
                        {
                            "kind": "official",
                            "url": "https://www.binance.com/en/support/faq/wallet",
                        }
                    ],
                },
                {
                    "chain": "bsc",
                    "address": BSC,
                    "entity": "binance_peg_reserve",
                    "label": "Binance reserve",
                    "role": "reserve",
                    "status": "trusted",
                    "verified_on": verified_on.isoformat(),
                    "evidence": [
                        {
                            "kind": "official",
                            "url": "https://www.binance.com/en/support/faq/reserve",
                        }
                    ],
                },
                {
                    "chain": "solana",
                    "address": SOL,
                    "entity": "fireblocks_custody",
                    "label": "Fireblocks",
                    "role": "custody",
                    "status": "trusted",
                    "verified_on": verified_on.isoformat(),
                    "evidence": [
                        {
                            "kind": "official",
                            "url": "https://www.fireblocks.com/customers/binance",
                        }
                    ],
                },
            ]
        }
    )


def observation(
    metric: str,
    scope: str,
    value: float,
    observed_at: datetime = NOW,
    *,
    quality: str = "FACT",
    metadata: dict | None = None,
) -> Observation:
    return Observation(
        metric,
        "test",
        scope,
        value,
        "USD1",
        observed_at,
        NOW,
        quality=quality,
        metadata=metadata or {},
    )


class FakeCustodySource:
    def __init__(
        self,
        balances: dict[str, float],
        *,
        incomplete: set[str] | None = None,
        safe_blocks: dict[str, int] | None = None,
        failures: set[str] | None = None,
        raise_chains: set[str] | None = None,
    ) -> None:
        self.balances = balances
        self.incomplete = incomplete or set()
        self.safe_blocks = safe_blocks or {"ethereum": 100, "bsc": 200}
        self.failures = failures or set()
        self.raise_chains = raise_chains or set()
        self.trusted_calls: list[tuple[str, tuple[str, ...]]] = []

    def _result(self, chain, addresses, trusted_addresses, collected_at):
        self.trusted_calls.append(
            (chain, tuple(item.address for item in trusted_addresses))
        )
        trusted = {
            item.address.casefold() if chain != "solana" else item.address
            for item in trusted_addresses
        }
        rows = []
        failures = []
        trusted_complete = chain not in self.incomplete
        for item in addresses:
            key = item.address if chain == "solana" else item.address.casefold()
            if key in self.failures:
                failures.append(
                    CustodyFailure(
                        chain,
                        item.address,
                        item.label,
                        CustodyDataError("offline"),
                    )
                )
                if key in trusted:
                    trusted_complete = False
                continue
            if key not in self.balances:
                continue
            rows.append(
                Observation(
                    "custody.address_balance",
                    "fake",
                    f"{chain}:{key}",
                    self.balances[key],
                    "USD1",
                    collected_at,
                    collected_at,
                    metadata={
                        "entity": item.entity,
                        "label": item.label,
                        "status": "trusted" if key in trusted else "candidate",
                        "verified_on": (
                            item.verified_on.isoformat()
                            if item.verified_on is not None
                            else None
                        ),
                    },
                )
            )
        return CustodyCollection(
            tuple(rows),
            tuple(failures),
            trusted_complete=trusted_complete,
            safe_block=(
                999 if chain == "solana" else self.safe_blocks.get(chain, 0)
            ),
        )

    async def collect_evm(
        self, chain, addresses, confirmation_depth, collected_at, *, trusted_addresses
    ):
        if chain in self.raise_chains:
            raise CustodyDataError(f"{chain} offline")
        return self._result(chain, addresses, trusted_addresses, collected_at)

    async def collect_solana(self, addresses, collected_at, *, trusted_addresses):
        if "solana" in self.raise_chains:
            raise CustodyDataError("solana offline")
        return self._result("solana", addresses, trusted_addresses, collected_at)


class FakeBlockRpc:
    async def call(self, method, params):
        assert method == "eth_getBlockByNumber"
        return {"number": params[0], "timestamp": hex(int(NOW.timestamp()))}


async def seed_supply(storage, value=100.0, when=NOW, quality="FACT") -> None:
    await storage.insert_observation(
        observation(
            "supply.multichain_total", "global", value, when, quality=quality
        )
    )


@pytest.mark.asyncio
async def test_custody_monitor_persists_scoped_balances_and_initial_red(storage) -> None:
    await seed_supply(storage)
    source = FakeCustodySource(
        {ETH: 40.0, BSC: 31.0, SOL: 7.0}
    )
    monitor = CustodyConcentrationMonitor(
        source, storage, custody_config(), timezone_name="UTC"
    )

    result = await monitor.check_once(deliver=False, now=NOW)

    assert result.success
    assert "trusted_balance=71" in result.details[0]
    assert "share_lower_bound=0.71" in result.details[0]
    share = await storage.latest_observation(
        "custody.binance_share_lower_bound", "global"
    )
    assert share is not None and share.value == pytest.approx(0.71)
    assert (
        await storage.latest_observation(
            "custody.address_balance", f"ethereum:{ETH}"
        )
        is not None
    )
    assert (
        await storage.latest_observation(
            "custody.address_balance", f"solana:{SOL}"
        )
        is not None
    )
    assert (
        await storage.latest_observation(
            "custody.entity_balance", "binance_cex:ethereum"
        )
    ).value == 40
    state = await storage.get_risk_state("custody.binance_concentration")
    assert state is not None and state.level is RiskLevel.RED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("balance", "expected"),
    [(50.0, RiskLevel.GREEN), (70.0, RiskLevel.YELLOW)],
)
async def test_custody_monitor_share_boundaries_are_strict(
    storage, balance, expected
) -> None:
    await seed_supply(storage)
    source = FakeCustodySource({ETH: balance, BSC: 0, SOL: 0})
    await CustodyConcentrationMonitor(
        source, storage, custody_config(), timezone_name="UTC"
    ).check_once(deliver=False, now=NOW)

    state = await storage.get_risk_state("custody.binance_concentration")
    assert state is not None and state.level is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("supply_problem", ["stale", "estimated"])
async def test_custody_monitor_does_not_publish_with_unusable_supply(
    storage, supply_problem
) -> None:
    await seed_supply(
        storage,
        when=NOW - timedelta(seconds=4501),
        quality="FACT" if supply_problem == "stale" else "ESTIMATED",
    )
    monitor = CustodyConcentrationMonitor(
        FakeCustodySource({ETH: 40, BSC: 31, SOL: 0}),
        storage,
        custody_config(),
        timezone_name="UTC",
    )

    result = await monitor.check_once(deliver=False, now=NOW)

    assert not result.success
    assert result.errors[0].startswith("custody:")
    assert await storage.latest_observation(
        "custody.binance_share_lower_bound", "global"
    ) is None


@pytest.mark.asyncio
async def test_custody_monitor_does_not_publish_partial_trusted_set(storage) -> None:
    await seed_supply(storage)
    monitor = CustodyConcentrationMonitor(
        FakeCustodySource({ETH: 40, SOL: 0}, incomplete={"bsc"}),
        storage,
        custody_config(),
        timezone_name="UTC",
    )

    result = await monitor.check_once(deliver=False, now=NOW)

    assert not result.success
    assert await storage.latest_observation(
        "custody.binance_verified_balance", "global"
    ) is None
    assert await storage.get_risk_state("custody.binance_concentration") is None


@pytest.mark.asyncio
async def test_safe_block_behind_scan_cursor_invalidates_trusted_snapshot(storage) -> None:
    await seed_supply(storage)
    await storage.set_scan_cursor("ethereum", 1000)
    await storage.set_scan_cursor("bsc", 200)
    monitor = CustodyConcentrationMonitor(
        FakeCustodySource(
            {ETH: 40, BSC: 31, SOL: 0},
            safe_blocks={"ethereum": 100, "bsc": 200},
        ),
        storage,
        custody_config(),
        timezone_name="UTC",
    )

    result = await monitor.check_once(deliver=False, now=NOW)

    assert not result.success
    assert any("safe block 100" in item and "cursor 1000" in item for item in result.errors)
    assert await storage.latest_observation(
        "custody.binance_verified_balance", "global"
    ) is None
    assert await storage.latest_observation(
        "custody.binance_share_lower_bound", "global"
    ) is None
    assert await storage.get_risk_state("custody.binance_concentration") is None


@pytest.mark.asyncio
async def test_custody_monitor_recomputes_expired_trust_at_runtime(storage) -> None:
    check_time = datetime(2026, 12, 11, tzinfo=UTC)
    await seed_supply(storage, when=check_time)
    config = custody_config(verified_on=date(2026, 9, 11))
    source = FakeCustodySource({ETH: 40, BSC: 31, SOL: 7})
    monitor = CustodyConcentrationMonitor(
        source, storage, config, timezone_name="UTC"
    )

    result = await monitor.check_once(deliver=False, now=check_time)

    assert not result.success
    assert any("no effective trusted addresses" in item for item in result.errors)
    assert all(not trusted for _, trusted in source.trusted_calls)
    assert await storage.latest_observation(
        "custody.binance_verified_balance", "global"
    ) is None


@pytest.mark.asyncio
async def test_non_binance_trust_cannot_turn_missing_binance_set_green(storage) -> None:
    check_time = datetime(2026, 12, 1, tzinfo=UTC)
    await seed_supply(storage, when=check_time)
    base = custody_config()
    addresses = [
        item.model_copy(
            update={
                "verified_on": (
                    date(2026, 12, 1)
                    if item.entity == "fireblocks_custody"
                    else date(2026, 8, 1)
                )
            }
        )
        for item in base.addresses
    ]
    config = base.model_copy(update={"addresses": addresses})
    monitor = CustodyConcentrationMonitor(
        FakeCustodySource({ETH: 40, BSC: 31, SOL: 7}),
        storage,
        config,
        timezone_name="UTC",
    )

    result = await monitor.check_once(deliver=False, now=check_time)

    assert not result.success
    assert any("Binance" in item for item in result.errors)
    assert await storage.latest_observation(
        "custody.binance_share_lower_bound", "global"
    ) is None
    assert await storage.get_risk_state("custody.binance_concentration") is None


@pytest.mark.asyncio
async def test_flow_marker_is_written_once_and_lag_keeps_windows_accumulating(
    storage,
) -> None:
    await seed_supply(storage)
    source = FakeCustodySource({ETH: 40, BSC: 31, SOL: 0})
    monitor = CustodyConcentrationMonitor(
        source,
        storage,
        custody_config(),
        evm_rpcs={"ethereum": FakeBlockRpc(), "bsc": FakeBlockRpc()},
        timezone_name="UTC",
    )

    first = await monitor.check_once(deliver=False, now=NOW)
    source.safe_blocks = {"ethereum": 110, "bsc": 210}
    second = await monitor.check_once(deliver=False, now=NOW + timedelta(minutes=10))

    marker = await storage.latest_observation("custody.flow_start_block", "ethereum")
    accumulating = await storage.latest_observation(
        "custody.binance_net_change_24h", "global"
    )
    assert first.success and second.success
    assert marker is not None and marker.value == 100
    assert accumulating is not None
    assert accumulating.metadata["status"] == "accumulating"
    assert await storage.get_risk_state("custody.binance_flow_24h") is None


@pytest.mark.asyncio
async def test_mature_flow_rules_restore_clear_count_per_rule(storage) -> None:
    await seed_supply(storage)
    config = custody_config()
    source = FakeCustodySource({ETH: 40, BSC: 31, SOL: 0})
    for chain, block in (("ethereum", 100), ("bsc", 200)):
        await storage.insert_observation(
            observation(
                "custody.flow_start_block",
                chain,
                block,
                NOW - timedelta(hours=25),
            )
        )
        await storage.insert_observation(
            observation(
                "custody.flow_coverage_start",
                chain,
                block,
                NOW - timedelta(hours=25),
            )
        )
        await storage.set_scan_cursor(chain, block)
    event = ChainEvent(
        "ethereum",
        100,
        "0xflow",
        0,
        "TRANSFER",
        {
            "from_address": "0x" + "99" * 20,
            "to_address": ETH,
            "amount": 60_000_001.0,
            "block_time": (NOW - timedelta(minutes=30)).isoformat(),
        },
        NOW,
    )
    await storage.insert_chain_events_and_cursor("ethereum", [event], 100)
    monitor = CustodyConcentrationMonitor(
        source,
        storage,
        config,
        evm_rpcs={"ethereum": FakeBlockRpc(), "bsc": FakeBlockRpc()},
        timezone_name="UTC",
    )

    await monitor.check_once(deliver=False, now=NOW)
    assert (
        await storage.get_risk_state("custody.binance_flow_24h")
    ).level is RiskLevel.YELLOW
    await storage.connection.execute("DELETE FROM chain_events")
    await storage.connection.commit()
    source.safe_blocks = {"ethereum": 101, "bsc": 201}
    await monitor.check_once(deliver=False, now=NOW + timedelta(minutes=10))
    gap_evidence = await storage.latest_observation(
        "custody.rule_state", "custody.binance_flow_24h"
    )
    assert gap_evidence.metadata["clear_checks"] == 0
    await storage.set_scan_cursor("ethereum", 101)
    await storage.set_scan_cursor("bsc", 201)
    await monitor.check_once(deliver=False, now=NOW + timedelta(minutes=20))
    state = await storage.get_risk_state("custody.binance_flow_24h")
    evidence = await storage.latest_observation(
        "custody.rule_state", "custody.binance_flow_24h"
    )
    assert state.level is RiskLevel.YELLOW
    assert evidence.metadata["clear_checks"] == 1
    await monitor.check_once(deliver=False, now=NOW + timedelta(minutes=30))
    assert (
        await storage.get_risk_state("custody.binance_flow_24h")
    ).level is RiskLevel.GREEN


@pytest.mark.asyncio
async def test_failed_round_interrupts_concentration_recovery_sequence(storage) -> None:
    await seed_supply(storage)
    source = FakeCustodySource({ETH: 60, BSC: 0, SOL: 0})
    monitor = CustodyConcentrationMonitor(
        source, storage, custody_config(), timezone_name="UTC"
    )
    await monitor.check_once(deliver=False, now=NOW)
    source.balances[ETH] = 40
    await monitor.check_once(deliver=False, now=NOW + timedelta(minutes=10))
    assert (
        await storage.latest_observation(
            "custody.rule_state", "custody.binance_concentration"
        )
    ).metadata["clear_checks"] == 1

    source.incomplete.add("bsc")
    failed = await monitor.check_once(
        deliver=False, now=NOW + timedelta(minutes=20)
    )
    source.incomplete.clear()
    await monitor.check_once(deliver=False, now=NOW + timedelta(minutes=30))

    state = await storage.get_risk_state("custody.binance_concentration")
    evidence = await storage.latest_observation(
        "custody.rule_state", "custody.binance_concentration"
    )
    assert not failed.success
    assert state is not None and state.level is RiskLevel.YELLOW
    assert evidence.metadata["clear_checks"] == 1


@pytest.mark.asyncio
async def test_mature_address_outflow_uses_stable_scopes_and_yellow_only(
    storage,
) -> None:
    await seed_supply(storage)
    for chain, block in (("ethereum", 100), ("bsc", 200)):
        for metric in ("custody.flow_start_block", "custody.flow_coverage_start"):
            await storage.insert_observation(
                observation(metric, chain, block, NOW - timedelta(hours=25))
            )
        await storage.set_scan_cursor(chain, block)
    await storage.insert_chain_events_and_cursor(
        "ethereum",
        [
            ChainEvent(
                "ethereum",
                99,
                "0xbefore-marker",
                0,
                "TRANSFER",
                {
                    "from_address": ETH,
                    "to_address": "0x" + "99" * 20,
                    "amount": 999_000_000.0,
                    "block_time": (NOW - timedelta(minutes=40)).isoformat(),
                },
                NOW,
            ),
            ChainEvent(
                "ethereum",
                100,
                "0xout",
                1,
                "TRANSFER",
                {
                    "from_address": ETH,
                    "to_address": "0x" + "99" * 20,
                    "amount": 110_000_001.0,
                    "block_time": (NOW - timedelta(minutes=30)).isoformat(),
                },
                NOW,
            ),
            ChainEvent(
                "ethereum",
                100,
                "0xin",
                2,
                "TRANSFER",
                {
                    "from_address": "0x" + "99" * 20,
                    "to_address": ETH,
                    "amount": 5_000_000.0,
                    "block_time": (NOW - timedelta(minutes=20)).isoformat(),
                },
                NOW,
            ),
        ],
        100,
    )
    monitor = CustodyConcentrationMonitor(
        FakeCustodySource({ETH: 40, BSC: 31, SOL: 0}),
        storage,
        custody_config(),
        evm_rpcs={"ethereum": FakeBlockRpc(), "bsc": FakeBlockRpc()},
        timezone_name="UTC",
    )

    await monitor.check_once(deliver=False, now=NOW)

    outflow = await storage.latest_observation(
        "custody.address_external_outflow_1h", f"ethereum:{ETH}"
    )
    state = await storage.get_risk_state("custody.address_outflow_1h")
    assert outflow is not None and outflow.value == pytest.approx(105_000_001)
    assert state is not None and state.level is RiskLevel.YELLOW
    rule_row = await storage.latest_observation(
        "custody.rule_state", "custody.address_outflow_1h"
    )
    assert rule_row is not None
    evidence = rule_row.metadata["evidence"]
    assert evidence["chains"] == ["ethereum", "bsc"]
    assert evidence["labels"][f"ethereum:{ETH}"] == "Binance ETH"
    assert evidence["labels"][f"bsc:{BSC}"] == "Binance reserve"
    assert f"https://etherscan.io/address/{ETH}" in evidence["source_urls"]
    assert "https://etherscan.io/block/100" in evidence["source_urls"]
    assert all("bscscan.com" not in url for url in evidence["source_urls"])
    pending = await storage.pending_alerts()
    address_alert = next(
        item.content for item in pending if "1 小时净流出" in item.content
    )
    assert "Ethereum Binance ETH 1 小时净流出 1.05000001 亿 USD1" in address_alert
    assert f"https://etherscan.io/address/{ETH}" in address_alert
    assert "custody.address_outflow_1h" not in address_alert


@pytest.mark.asyncio
async def test_flow_alert_uses_only_actual_trusted_chain_and_human_label(
    storage,
) -> None:
    await seed_supply(storage, value=200_000_000)
    base = custody_config()
    candidate_bsc = base.addresses[1].model_copy(
        update={"status": "candidate", "verified_on": None}
    )
    config = base.model_copy(
        update={"addresses": [base.addresses[0], candidate_bsc]}
    )
    for metric in ("custody.flow_start_block", "custody.flow_coverage_start"):
        await storage.insert_observation(
            observation(metric, "ethereum", 100, NOW - timedelta(hours=25))
        )
    await storage.set_scan_cursor("ethereum", 100)
    await storage.insert_chain_events_and_cursor(
        "ethereum",
        [
            ChainEvent(
                "ethereum",
                100,
                "0xinflow",
                0,
                "TRANSFER",
                {
                    "from_address": "0x" + "99" * 20,
                    "to_address": ETH,
                    "amount": 60_000_000.0,
                    "block_time": (NOW - timedelta(minutes=30)).isoformat(),
                },
                NOW,
            )
        ],
        100,
    )
    monitor = CustodyConcentrationMonitor(
        FakeCustodySource({ETH: 40_000_000, BSC: 150_000_000}),
        storage,
        config,
        timezone_name="UTC",
    )

    await monitor.check_once(deliver=False, now=NOW)

    rule_row = await storage.latest_observation(
        "custody.rule_state", "custody.binance_flow_24h"
    )
    assert rule_row is not None
    evidence = rule_row.metadata["evidence"]
    assert evidence["chains"] == ["ethereum"]
    assert evidence["labels"] == {f"ethereum:{ETH}": "Binance ETH"}
    assert evidence["source_urls"] == ["https://etherscan.io/block/100"]
    pending = await storage.pending_alerts()
    assert len(pending) == 1
    alert = pending[0].content
    assert "Ethereum 已核验 Binance 地址组 24 小时净流入 6000 万 USD1" in alert
    assert "https://etherscan.io/block/100" in alert
    assert "BNB Chain" not in alert
    assert "Binance reserve" not in alert


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prior_time",
    [
        NOW - timedelta(hours=24, minutes=5),
        NOW - timedelta(hours=23, minutes=55),
    ],
)
async def test_solana_only_publishes_unattributed_snapshot_deltas(
    storage, prior_time
) -> None:
    await seed_supply(storage)
    await storage.insert_observation(
        observation(
            "custody.address_balance",
            f"solana:{SOL}",
            5,
            prior_time,
            metadata={
                "entity": "fireblocks_custody",
                "status": "trusted",
                "verified_on": "2026-09-11",
            },
        )
    )
    monitor = CustodyConcentrationMonitor(
        FakeCustodySource({ETH: 40, BSC: 31, SOL: 7}),
        storage,
        custody_config(),
        timezone_name="UTC",
    )

    await monitor.check_once(deliver=False, now=NOW)

    delta = await storage.latest_observation(
        "custody.solana_balance_delta_24h", f"solana:{SOL}"
    )
    assert delta is not None and delta.value == 2
    assert delta.metadata["counterparty_attribution"] is False
    assert "counterparty" not in delta.metadata


@pytest.mark.asyncio
async def test_solana_delta_rejects_snapshot_far_before_window_boundary(storage) -> None:
    await seed_supply(storage)
    await storage.insert_observation(
        observation(
            "custody.address_balance",
            f"solana:{SOL}",
            5,
            NOW - timedelta(hours=26),
            metadata={
                "entity": "fireblocks_custody",
                "status": "trusted",
                "verified_on": "2026-09-11",
            },
        )
    )
    monitor = CustodyConcentrationMonitor(
        FakeCustodySource({ETH: 40, BSC: 31, SOL: 7}),
        storage,
        custody_config(),
        timezone_name="UTC",
    )

    await monitor.check_once(deliver=False, now=NOW)

    delta = await storage.latest_observation(
        "custody.solana_balance_delta_24h", f"solana:{SOL}"
    )
    assert delta is not None
    assert delta.quality == "UNAVAILABLE"
    assert delta.metadata["status"] == "accumulating"


@pytest.mark.asyncio
@pytest.mark.parametrize("interval_seconds", [3600, 43200])
async def test_solana_long_interval_cannot_reuse_current_snapshot_as_history(
    storage, interval_seconds
) -> None:
    await seed_supply(storage)
    config = custody_config().model_copy(
        update={"interval_seconds": interval_seconds}
    )
    monitor = CustodyConcentrationMonitor(
        FakeCustodySource({ETH: 40, BSC: 31, SOL: 7}),
        storage,
        config,
        timezone_name="UTC",
    )

    await monitor.check_once(deliver=False, now=NOW)

    for hours in (1, 24):
        delta = await storage.latest_observation(
            f"custody.solana_balance_delta_{hours}h", f"solana:{SOL}"
        )
        assert delta is not None and delta.quality == "UNAVAILABLE"
        assert delta.metadata["status"] == "accumulating"


@pytest.mark.asyncio
async def test_candidate_solana_address_does_not_publish_delta(storage) -> None:
    await seed_supply(storage)
    base = custody_config()
    addresses = [
        item.model_copy(update={"status": "candidate"})
        if item.chain == "solana"
        else item
        for item in base.addresses
    ]
    config = base.model_copy(update={"addresses": addresses})
    monitor = CustodyConcentrationMonitor(
        FakeCustodySource({ETH: 40, BSC: 31, SOL: 7}),
        storage,
        config,
        timezone_name="UTC",
    )

    await monitor.check_once(deliver=False, now=NOW)

    assert await storage.latest_observation(
        "custody.solana_balance_delta_1h", f"solana:{SOL}"
    ) is None
    assert await storage.latest_observation(
        "custody.solana_balance_delta_24h", f"solana:{SOL}"
    ) is None


@pytest.mark.asyncio
async def test_candidate_solana_failure_does_not_block_trusted_concentration(
    storage,
) -> None:
    await seed_supply(storage)
    base = custody_config()
    addresses = [
        item.model_copy(update={"status": "candidate"})
        if item.chain == "solana"
        else item
        for item in base.addresses
    ]
    config = base.model_copy(update={"addresses": addresses})
    monitor = CustodyConcentrationMonitor(
        FakeCustodySource(
            {ETH: 40, BSC: 31, SOL: 7}, failures={SOL}
        ),
        storage,
        config,
        timezone_name="UTC",
    )

    result = await monitor.check_once(deliver=False, now=NOW)

    assert result.success
    assert (
        await storage.latest_observation(
            "custody.binance_share_lower_bound", "global"
        )
    ).value == pytest.approx(0.71)
    assert (
        await storage.get_risk_state("custody.binance_concentration")
    ).level is RiskLevel.RED
    assert await storage.latest_observation(
        "custody.solana_balance_delta_1h", f"solana:{SOL}"
    ) is None
    assert await storage.latest_observation(
        "custody.rule_interruption", "custody.binance_concentration"
    ) is None
    assert await storage.latest_observation(
        "custody.solana_balance_delta_24h", f"solana:{SOL}"
    ) is None
    for rule_id in ("custody.binance_flow_24h", "custody.address_outflow_1h"):
        interruption = await storage.latest_observation(
            "custody.rule_interruption", rule_id
        )
        assert interruption is not None
        assert interruption.metadata["reason"] == "custody flow window unavailable"


@pytest.mark.asyncio
async def test_candidate_only_chain_exception_does_not_block_concentration(
    storage,
) -> None:
    await seed_supply(storage)
    base = custody_config()
    config = base.model_copy(
        update={
            "addresses": [
                item.model_copy(update={"status": "candidate"})
                if item.chain == "solana"
                else item
                for item in base.addresses
            ]
        }
    )
    monitor = CustodyConcentrationMonitor(
        FakeCustodySource(
            {ETH: 40, BSC: 31},
            raise_chains={"solana"},
        ),
        storage,
        config,
        timezone_name="UTC",
    )

    result = await monitor.check_once(deliver=False, now=NOW)

    assert result.success
    assert (
        await storage.latest_observation(
            "custody.binance_share_lower_bound", "global"
        )
    ).value == pytest.approx(0.71)


class FakeComponent:
    def __init__(self, result: CheckResult) -> None:
        self.result = result

    async def check_once(self, *, deliver=False):
        return self.result


class RaisingComponent:
    def __init__(self) -> None:
        self.interruptions: list[str] = []

    async def check_once(self, *, deliver=False):
        raise RuntimeError("custody RPC offline")

    async def record_interruption(self, now, reason):
        self.interruptions.append(reason)


class InterruptionFailingComponent(RaisingComponent):
    async def record_interruption(self, now, reason):
        raise RuntimeError("database locked")


class SlowCustody:
    def __init__(self, real_monitor) -> None:
        self.real_monitor = real_monitor

    async def check_once(self, *, deliver=False):
        import asyncio

        await asyncio.sleep(1)
        return CheckResult(True, ())

    async def record_interruption(self, now, reason):
        await self.real_monitor.record_interruption(now, reason)


@pytest.mark.asyncio
async def test_custody_failure_does_not_block_other_components_or_alert(storage) -> None:
    monitor = Usd1Monitor(
        FakeComponent(CheckResult(True, (), ("market ok",))),
        [],
        storage,
        None,
        custody=FakeComponent(CheckResult(False, ("custody: unavailable",))),
    )

    result = await monitor.check_once(deliver=False)

    assert "market ok" in result.details
    assert "custody: unavailable" in result.errors
    health = await storage.get_risk_state("health.scheduler_custody")
    assert health is not None
    assert await storage.count_alert_deliveries() == 0


@pytest.mark.asyncio
async def test_unexpected_custody_exception_is_isolated_as_health_failure(storage) -> None:
    custody = RaisingComponent()
    monitor = Usd1Monitor(
        FakeComponent(CheckResult(True, (), ("market ok",))),
        [],
        storage,
        None,
        custody=custody,
    )

    result = await monitor.check_once(deliver=False)

    assert "market ok" in result.details
    assert result.errors == ("custody: RuntimeError: custody RPC offline",)
    assert await storage.get_risk_state("health.scheduler_custody") is not None
    assert await storage.count_alert_deliveries() == 0
    assert custody.interruptions == ["custody: RuntimeError: custody RPC offline"]


@pytest.mark.asyncio
async def test_interruption_failure_preserves_original_custody_error(storage) -> None:
    custody = InterruptionFailingComponent()
    monitor = Usd1Monitor(
        FakeComponent(CheckResult(True, ())),
        [],
        storage,
        None,
        custody=custody,
    )

    result = await monitor.check_once(deliver=False)

    assert result.errors[0] == "custody: RuntimeError: custody RPC offline"
    assert result.errors[1] == (
        "custody interruption recording failed: RuntimeError: database locked"
    )


@pytest.mark.asyncio
async def test_top_level_timeout_interrupts_concentration_clear_sequence(storage) -> None:
    base = datetime.now(UTC) - timedelta(minutes=20)
    await seed_supply(storage, when=base)
    source = FakeCustodySource({ETH: 60, BSC: 0, SOL: 0})
    real_custody = CustodyConcentrationMonitor(
        source, storage, custody_config(), timezone_name="UTC"
    )
    await real_custody.check_once(deliver=False, now=base)
    source.balances[ETH] = 40
    await real_custody.check_once(deliver=False, now=base + timedelta(minutes=10))
    slow = SlowCustody(real_custody)
    top = Usd1Monitor(
        FakeComponent(CheckResult(True, ())),
        [],
        storage,
        None,
        custody=slow,
        check_timeout_seconds=0.01,
    )

    timed_out = await top._check_component_once(
        "custody", lambda: slow.check_once(deliver=False)
    )
    for rule_id in (
        "custody.binance_concentration",
        "custody.binance_flow_24h",
        "custody.address_outflow_1h",
    ):
        interruption = await storage.latest_observation(
            "custody.rule_interruption", rule_id
        )
        assert interruption is not None
        assert "TimeoutError" in interruption.metadata["reason"]
    await storage.insert_observation(
        observation(
            "supply.multichain_total",
            "global",
            100,
            datetime.now(UTC),
        )
    )
    await real_custody.check_once(
        deliver=False, now=datetime.now(UTC) + timedelta(seconds=1)
    )

    assert not timed_out.success and "TimeoutError" in timed_out.errors[0]
    state = await storage.get_risk_state("custody.binance_concentration")
    evidence = await storage.latest_observation(
        "custody.rule_state", "custody.binance_concentration"
    )
    assert state is not None and state.level is RiskLevel.YELLOW
    assert evidence.metadata["clear_checks"] == 1
