from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from dotenv import load_dotenv

from usd1_monitor.collectors.evm import (
    EvmScanner,
    EvmSnapshotReader,
    PrivilegedCallCollector,
)
from usd1_monitor.collectors.announcements import (
    BinanceAnnouncementCollector,
    OfficialPageCollector,
    parse_bitgo_items,
    parse_occ_items,
    parse_wlfi_items,
)
from usd1_monitor.collectors.market import BinanceMarketCollector
from usd1_monitor.collectors.reserves import PorCollector
from usd1_monitor.collectors.supply import (
    DefiLlamaSupplyCollector,
    NativeSupplyCollector,
)
from usd1_monitor.config import AppConfig, ConfigError, load_config
from usd1_monitor.http import AsyncHttpClient
from usd1_monitor.logging_config import configure_logging
from usd1_monitor.engine.information_rules import next_attestation_due_at
from usd1_monitor.notifications.wechat import WeChatNotifier
from usd1_monitor.rpc import JsonRpcClient
from usd1_monitor.scheduler import (
    CombinedSupplySource,
    ConfirmedPorSource,
    EvmChainMonitor,
    InformationMonitor,
    MarketMonitor,
    NOT_MONITORED,
    ReserveSupplyMonitor,
    Usd1Monitor,
)
from usd1_monitor.engine.aggregate import business_overall, health_overall
from usd1_monitor.models import RiskLevel
from usd1_monitor.time_utils import local_iso
from usd1_monitor.storage import Storage


class Closable(Protocol):
    async def close(self) -> None: ...


MonitorBuilder = Callable[[AppConfig, Storage], tuple[object, Closable]]


def build_market_monitor(
    config: AppConfig, storage: Storage
) -> tuple[Usd1Monitor, AsyncHttpClient]:
    http = AsyncHttpClient(config.http)
    collector = BinanceMarketCollector(
        http,
        sell_sizes=config.market.sell_sizes,
        depth_limit=config.market.depth_limit,
    )
    notifier = (
        WeChatNotifier(config.wechat_webhook, http)
        if config.wechat_webhook
        else None
    )
    market = MarketMonitor(collector, storage, None, config.market)
    evm_chains: list[EvmChainMonitor] = []
    rpc_by_chain: dict[str, JsonRpcClient] = {}
    for chain_name in ("ethereum", "bsc"):
        chain_config = getattr(config.chains, chain_name)
        rpc = JsonRpcClient(
            chain_config.rpc_urls,
            http,
            expected_chain_id=chain_config.chain_id,
        )
        rpc_by_chain[chain_name] = rpc
        watched = {
            item.address
            for item in config.watched_addresses
            if item.chain == chain_name
        }
        labels = tuple(
            item.label
            for item in config.watched_addresses
            if item.chain == chain_name
        )
        scanner = EvmScanner(
            chain_name,
            rpc,
            storage,
            confirmation_depth=chain_config.confirmation_depth,
            overlap_blocks=chain_config.overlap_blocks,
            batch_blocks=chain_config.scan_batch_blocks,
            log_query_chunk_blocks=chain_config.log_query_chunk_blocks,
            token_address=chain_config.token_address,
        )
        evm_chains.append(
            EvmChainMonitor(
                chain_name,
                scanner,
                EvmSnapshotReader(
                    chain_name,
                    rpc,
                    chain_config.token_address,
                    watched_addresses=watched,
                ),
                storage,
                watched_addresses=watched,
                privileged_collector=PrivilegedCallCollector(
                    chain_name, rpc, chain_config.token_address
                ),
                watched_labels=labels,
                interval_seconds=chain_config.interval_seconds,
            )
        )
    ethereum = config.chains.ethereum
    reserve_supply = ReserveSupplyMonitor(
        ConfirmedPorSource(
            rpc_by_chain["ethereum"],
            PorCollector(rpc_by_chain["ethereum"], config.por.address),
            ethereum.confirmation_depth,
        ),
        CombinedSupplySource(
            [
                (
                    rpc_by_chain[chain_name],
                    NativeSupplyCollector(
                        chain_name,
                        rpc_by_chain[chain_name],
                        getattr(config.chains, chain_name).token_address,
                    ),
                    getattr(config.chains, chain_name).confirmation_depth,
                )
                for chain_name in ("ethereum", "bsc")
            ],
            DefiLlamaSupplyCollector(
                http,
                url=config.supply.defillama_url,
                expected_symbol=config.supply.expected_symbol,
                expected_name=config.supply.expected_name,
            ),
        ),
        storage,
        None,
        por_config=config.por,
        supply_config=config.supply,
    )
    parsers = {
        "bitgo": parse_bitgo_items,
        "wlfi": parse_wlfi_items,
        "occ": parse_occ_items,
    }
    official_sources = {
        source: OfficialPageCollector(
            source,
            getattr(config.information, source).url,
            http,
            parser,
        )
        for source, parser in parsers.items()
    }
    official_sources["binance"] = BinanceAnnouncementCollector(
        config.information.binance.url,
        http,
        known_ids_provider=(
            lambda: storage.announcement_stable_ids("binance")
        ),
        scanned_ids_provider=(
            lambda: storage.announcement_stable_ids("binance_scan")
        ),
        recent_scanned_ids_provider=(
            lambda since: storage.recent_announcement_stable_ids(
                "binance_scan", since
            )
        ),
        recent_failed_ids_provider=(
            lambda since: storage.recent_announcement_failure_ids(
                "binance_scan", since
            )
        ),
        scan_record_writer=(lambda item: storage.upsert_announcement(item)),
    )
    information = InformationMonitor(
        official_sources,
        storage,
        None,
        intervals={
            source: getattr(config.information, source).interval_seconds
            for source in official_sources
        },
    )
    return (
        Usd1Monitor(
            market,
            evm_chains,
            storage,
            notifier,
            interval_seconds=config.market.interval_seconds,
            reserve_supply=reserve_supply,
            information=information,
            retention_days=config.retention_days,
        ),
        http,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m usd1_monitor")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("command", choices=("check", "run", "status"))
    return parser


async def async_main(
    argv: Sequence[str] | None = None,
    *,
    monitor_builder: MonitorBuilder | None = None,
) -> int:
    args = _parser().parse_args(argv)
    load_dotenv(args.config.resolve().parent / ".env", override=False)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.command == "run":
        configure_logging(
            config.logging.path,
            max_bytes=config.logging.max_bytes,
            backup_count=config.logging.backup_count,
            timezone_name=config.timezone,
        )

    storage = Storage(
        config.database_path,
        timezone_name=config.timezone,
        event_active_seconds=config.event_active_seconds,
    )
    await storage.open()
    resource: Closable | None = None
    try:
        if args.command == "status":
            await _print_status(storage, timezone_name=config.timezone)
            return 0

        builder = monitor_builder or build_market_monitor
        monitor, resource = builder(config, storage)
        if args.command == "check":
            result = await monitor.check_once(deliver=False)  # type: ignore[attr-defined]
            for detail in result.details:
                print(detail)
            for error in result.errors:
                print(error, file=sys.stderr)
            return 0 if result.success else 1

        await monitor.run()  # type: ignore[attr-defined]
        return 0
    finally:
        if resource is not None:
            await resource.close()
        await storage.close()


async def _print_status(
    storage: Storage, *, timezone_name: str = "Asia/Shanghai"
) -> None:
    states = await storage.list_risk_states()
    business_states = [
        state for state in states if not state.rule_id.startswith("health.")
    ]
    health_states = [
        state for state in states if state.rule_id.startswith("health.")
    ]
    failed_alerts = await storage.failed_alerts()
    business_level = (
        business_overall(
            business_states,
            event_active_seconds=storage.event_active_seconds,
        ).name
        if business_states
        else "UNKNOWN"
    )
    if health_states:
        monitor_health = health_overall(health_states)
        if failed_alerts:
            monitor_health = max(monitor_health, RiskLevel.YELLOW)
        health_level = monitor_health.name
    else:
        health_level = "YELLOW" if failed_alerts else "UNKNOWN"
    print(f"business_overall: {business_level}")
    print(f"monitor_health: {health_level}")
    print(f"failed_alerts: {len(failed_alerts)}")
    for item in states:
        print(
            f"rule {item.rule_id}: {item.level.name} "
            f"changed_at={local_iso(item.changed_at, timezone_name)}"
        )
    for item in await storage.list_collector_health():
        last_success = (
            local_iso(item.last_success_at, timezone_name)
            if item.last_success_at
            else "UNKNOWN"
        )
        print(
            f"collector {item.collector_id}: last_success={last_success} "
            f"failures={item.consecutive_failures}"
        )
    monitored_metrics = (
        ("por.reserves", "ethereum"),
        ("supply.native", "ethereum"),
        ("supply.native", "bsc"),
        ("supply.global", "global"),
        ("supply.estimated_collateralization", "global"),
    )
    for metric, scope in monitored_metrics:
        observation = await storage.latest_observation(metric, scope)
        if observation is None:
            print(f"metric {metric}: UNKNOWN scope={scope}")
        else:
            print(
                f"metric {metric}: {observation.quality} "
                f"value={observation.value:g} scope={scope} "
                f"data_time={local_iso(observation.observed_at, timezone_name)}"
            )
    for event in await storage.recent_chain_events():
        print(
            f"evm_fact {event.chain}: {event.event_type} "
            f"block={event.block_number} tx={event.tx_hash}"
        )
    for source in ("binance", "bitgo", "wlfi", "occ"):
        item = await storage.latest_announcement(source)
        if item is None:
            print(f"official_{source}: UNKNOWN")
            continue
        print(
            f"official_{source}: {item.stable_id} title={item.title} url={item.url}"
        )
        if source == "bitgo":
            month = item.stable_id.split(":", 1)[0]
            try:
                print(
                    f"attestation latest_month={month} next_due={next_attestation_due_at(month)}"
                )
            except ValueError:
                print("attestation latest_month=UNKNOWN next_due=UNKNOWN")
    for item in NOT_MONITORED:
        print(f"NOT_MONITORED: {item}")


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(async_main(argv))
