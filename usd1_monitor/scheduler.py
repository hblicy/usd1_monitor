from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import Protocol
from urllib.parse import urlsplit

from usd1_monitor.config import (
    CustodyAddressConfig,
    CustodyConfig,
    MarketConfig,
    RedemptionConfig,
    current_date_for_timezone,
)
from usd1_monitor.collectors.custody import (
    CustodyBalanceCollector,
    CustodyCollection,
    enrich_transfer_timestamps,
)
from usd1_monitor.collectors.redemption import (
    RedemptionStatusSnapshot,
    _canonical_page_url,
)
from usd1_monitor.collectors.evm import (
    EvmScanner,
    EvmSnapshot,
    EvmSnapshotReader,
    PrivilegedCallCollector,
)
from usd1_monitor.collectors.announcements import (
    BinancePartialCollectionError,
    OfficialPageCollector,
    normalize_text,
)
from usd1_monitor.collectors.reserves import PorCollector, PorSnapshot
from usd1_monitor.collectors.supply import (
    DefiLlamaSupplyCollector,
    SupplyDataError,
    SupplySnapshot,
    estimated_coverage,
)
from usd1_monitor.collectors.multichain_supply import (
    REQUIRED_COMPONENT_IDS,
    MultichainSupplySource,
)
from usd1_monitor.config import PorConfig, SupplyConfig
from usd1_monitor.engine.evm_rules import (
    EXPLORER_BASE_URLS,
    EvmFact,
    evaluate_evm_fact,
)
from usd1_monitor.engine.health import evaluate_health_with_recovery
from usd1_monitor.engine.custody_rules import (
    RuleDecision,
    Transfer,
    evaluate_address_outflow,
    evaluate_concentration,
    evaluate_entity_flow,
    summarize_transfers,
)
from usd1_monitor.engine.information_rules import (
    classify_official_text,
    next_attestation_due_at,
)
from usd1_monitor.engine.redemption_rules import (
    RedemptionClassification,
    apply_recovery,
    classify_redemption,
)
from usd1_monitor.engine.reserve_rules import (
    PorReading,
    evaluate_por,
    evaluate_reserve_change,
)
from usd1_monitor.engine.supply_rules import (
    BridgeReading,
    SupplyRiskInput,
    evaluate_bridge_reconciliation,
    evaluate_supply,
)
from usd1_monitor.engine.market_rules import MarketSnapshot, evaluate_market
from usd1_monitor.engine.state import StateEngine
from usd1_monitor.evm_abi import DecodedEvent
from usd1_monitor.models import (
    ChainEvent,
    Announcement,
    CoverageState,
    Observation,
    RiskLevel,
    RuleEvaluation,
)
from usd1_monitor.notifications.wechat import CHAIN_LABELS, format_startup_message
from usd1_monitor.storage import Storage


logger = logging.getLogger(__name__)
_NOT_COLLECTED = object()
_NEW_INFORMATION_ALERT_MAX_AGE = timedelta(hours=24)
_CUSTODY_RULE_IDS = (
    "custody.binance_concentration",
    "custody.binance_flow_24h",
    "custody.address_outflow_1h",
)

NOT_MONITORED = (
    "private_exchange_account",
    "active_conversion_probe",
    "tron_solana_aptos_tempo_bridges",
    "social_media_sentiment",
    "defi_liquidations",
)

MUTABLE_EVM_EVENT_TYPES = (
    "FREEZE",
    "UNFREEZE",
    "PAUSED",
    "UNPAUSED",
    "PRIVILEGED_PAUSE",
    "PRIVILEGED_UNPAUSE",
)


class MarketCollector(Protocol):
    async def collect_symbol(
        self, symbol: str, collected_at: datetime
    ) -> list[Observation]: ...


class Notifier(Protocol):
    async def send_text(self, content: str) -> None: ...


class DeliveryRateLimiter:
    def __init__(
        self,
        storage: Storage,
        *,
        channel: str = "notification_wechat",
        max_messages: int = 20,
        window_seconds: float = 60,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if max_messages <= 0 or window_seconds <= 0:
            raise ValueError("delivery rate limit values must be positive")
        self._storage = storage
        self._channel = channel
        self._max_messages = max_messages
        self._window_seconds = window_seconds
        self._clock = clock

    async def try_acquire(self) -> bool:
        return await self._storage.try_acquire_delivery_slot(
            self._channel,
            self._clock(),
            max_messages=self._max_messages,
            window_seconds=self._window_seconds,
        )

    async def claim(self, alert_id: int) -> tuple[bool, bool]:
        return await self._storage.claim_alert_delivery_with_slot(
            alert_id,
            self._channel,
            self._clock(),
            max_messages=self._max_messages,
            window_seconds=self._window_seconds,
        )

    async def defer(self) -> None:
        await self._storage.defer_delivery(
            self._channel,
            self._clock() + timedelta(seconds=self._window_seconds),
        )


def _market_size_label(size: float) -> str:
    if size.is_integer():
        return str(int(size))
    return format(size, "f").rstrip("0").rstrip(".")


def _observation_source_urls(*items: Observation) -> list[str]:
    urls: list[str] = []
    for item in items:
        multiple = item.metadata.get("source_urls")
        candidates = (
            multiple
            if isinstance(multiple, (list, tuple))
            else [item.metadata.get("source_url")]
        )
        for value in candidates:
            if isinstance(value, str) and value and value not in urls:
                urls.append(value)
    return urls


def _validated_observation_age(observed_at: datetime, now: datetime) -> float:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observation timestamp must be timezone-aware")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("current timestamp must be timezone-aware")
    age = (now.astimezone(UTC) - observed_at.astimezone(UTC)).total_seconds()
    if age < 0:
        raise ValueError("observation timestamp must not be in the future")
    return age


@dataclass(frozen=True)
class CheckResult:
    success: bool
    errors: tuple[str, ...]
    details: tuple[str, ...] = ()


class RedemptionStatusSource(Protocol):
    async def collect(self, checked_at: datetime) -> RedemptionStatusSnapshot: ...


class RedemptionPageSource(Protocol):
    source: str

    async def collect(self, checked_at: datetime) -> Announcement: ...


class RedemptionMediaSource(Protocol):
    async def collect(self, checked_at: datetime) -> list[Announcement]: ...


class MarketMonitor:
    def __init__(
        self,
        collector: MarketCollector,
        storage: Storage,
        notifier: Notifier | None,
        market_config: MarketConfig | None = None,
    ) -> None:
        self._collector = collector
        self._storage = storage
        self._notifier = notifier
        self._config = market_config or MarketConfig()
        self._history: list[MarketSnapshot] = []
        self._startup_sent = False

    async def check_once(self, *, deliver: bool = True) -> CheckResult:
        collected_at = datetime.now(UTC)
        observations: list[Observation] = []
        errors: list[str] = []
        for symbol in self._config.symbols:
            try:
                observations.extend(
                    await self._collector.collect_symbol(symbol, collected_at)
                )
            except Exception as exc:
                logger.exception(
                    "market collection failed collector=binance symbol=%s", symbol
                )
                errors.append(f"{symbol}: {type(exc).__name__}: {exc}")

        if errors:
            await _record_health(
                self._storage,
                "binance_market",
                collected_at,
                success=False,
                error="; ".join(errors),
                critical=True,
            )
            return CheckResult(False, tuple(errors))

        try:
            current = self._build_snapshot(observations)
        except ValueError as exc:
            await _record_health(
                self._storage,
                "binance_market",
                collected_at,
                success=False,
                error=str(exc),
                critical=True,
            )
            return CheckResult(False, (str(exc),))

        if not self._history:
            history_window = self._history_window_seconds()
            terminal_metrics = tuple(
                f"market.sell_{_market_size_label(size)}_terminal_price"
                for size in self._config.sell_sizes
            )
            stored = await self._storage.observations_since(
                (
                    "market.symbol_trading",
                    "market.mid_price",
                    *terminal_metrics,
                ),
                current.observed_at - timedelta(seconds=history_window),
            )
            grouped: dict[datetime, list[Observation]] = {}
            for observation in stored:
                grouped.setdefault(observation.observed_at, []).append(observation)
            self._history = [
                self._build_snapshot(
                    grouped[observed_at], require_all_exit_sizes=False
                )
                for observed_at in sorted(grouped)
            ]
        self._history.append(current)
        history_cutoff = current.observed_at - timedelta(
            seconds=self._history_window_seconds()
        )
        self._history = [
            snapshot
            for snapshot in self._history
            if snapshot.observed_at >= history_cutoff
        ]
        previous_states = {
            rule_id: await self._storage.get_risk_state(rule_id)
            for rule_id in (
                "market.price",
                "market.price.severe",
                "market.liquidity",
                "market.trading",
            )
        }
        evaluation = evaluate_market(
            self._history,
            yellow_price=self._config.yellow_price,
            yellow_seconds=self._config.yellow_seconds,
            red_price=self._config.red_price,
            red_seconds=self._config.red_seconds,
            severe_price=self._config.severe_price,
            severe_seconds=self._config.severe_seconds,
            recovery_price=self._config.recovery_price,
            recovery_seconds=self._config.recovery_seconds,
            max_gap_seconds=max(90, int(self._config.interval_seconds * 1.5)),
            initial_price_level=(
                previous_states["market.price"].level
                if previous_states["market.price"] is not None
                else RiskLevel.GREEN
            ),
            initial_liquidity_level=(
                previous_states["market.liquidity"].level
                if previous_states["market.liquidity"] is not None
                else RiskLevel.GREEN
            ),
            initial_trading_level=(
                previous_states["market.trading"].level
                if previous_states["market.trading"] is not None
                else RiskLevel.GREEN
            ),
            initial_severe_level=(
                previous_states["market.price.severe"].level
                if previous_states["market.price.severe"] is not None
                else RiskLevel.GREEN
            ),
        )
        evaluations = [
                RuleEvaluation(
                    "market.price",
                    evaluation.price_level,
                    evaluation.evidence["market.price"],
                    f"market:{current.observed_at.isoformat()}",
                ),
                RuleEvaluation(
                    "market.price.severe",
                    evaluation.severe_level,
                    evaluation.evidence["market.price.severe"],
                    f"market:{current.observed_at.isoformat()}",
                ),
                RuleEvaluation(
                    "market.liquidity",
                    evaluation.liquidity_level,
                    evaluation.evidence["market.liquidity"],
                    f"market:{current.observed_at.isoformat()}",
                ),
                RuleEvaluation(
                    "market.trading",
                    evaluation.trading_level,
                    evaluation.evidence["market.trading"],
                    f"market:{current.observed_at.isoformat()}",
                ),
            ]
        connection = self._storage.connection
        async with self._storage.write_lock:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                for observation in observations:
                    await self._storage.insert_observation_uncommitted(observation)
                await StateEngine(self._storage).apply_uncommitted(
                    evaluations, current.observed_at
                )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                self._history.pop()
                raise
        await _record_health(
            self._storage,
            "binance_market",
            collected_at,
            success=True,
            critical=True,
        )
        if deliver and self._notifier is not None:
            await self._deliver_pending()
        return CheckResult(
            True,
            (),
            (
                f"binance_market symbols={','.join(self._config.symbols)} "
                f"data_time={current.observed_at.isoformat()}",
            ),
        )

    def _history_window_seconds(self) -> int:
        return max(
            self._config.yellow_seconds,
            self._config.red_seconds,
            self._config.severe_seconds,
            self._config.recovery_seconds,
        ) + max(90, int(self._config.interval_seconds * 1.5))

    async def _deliver_pending(self) -> None:
        if self._notifier is None:
            return
        await _deliver_pending(self._storage, self._notifier)

    async def send_startup_once(self) -> None:
        if self._startup_sent or self._notifier is None:
            return
        content = format_startup_message(
            ("价格", "流动性", "交易状态"),
            NOT_MONITORED,
        )
        await self._notifier.send_text(content)
        self._startup_sent = True

    async def run(self) -> None:
        if self._notifier is None:
            logger.warning("notifications are disabled: WECHAT_WEBHOOK is not configured")
        await self.send_startup_once()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        installed_signals: list[signal.Signals] = []
        for name in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(name, stop.set)
                installed_signals.append(name)
            except (NotImplementedError, RuntimeError):
                pass

        next_tick = loop.time()
        try:
            while not stop.is_set():
                await self.check_once()
                next_tick += self._config.interval_seconds
                timeout = max(0.0, next_tick - loop.time())
                try:
                    await asyncio.wait_for(stop.wait(), timeout=timeout)
                except TimeoutError:
                    pass
        finally:
            for name in installed_signals:
                loop.remove_signal_handler(name)

    def _build_snapshot(
        self,
        observations: list[Observation],
        *,
        require_all_exit_sizes: bool = True,
    ) -> MarketSnapshot:
        indexed = {(item.scope, item.metric): item for item in observations}
        mids: dict[str, float] = {}
        trading: dict[str, bool] = {}
        terminal: dict[str, float] = {}
        fillable: dict[str, bool] = {}
        exit_capacity: dict[str, dict[str, dict[str, object]]] = {}
        times: list[datetime] = []
        terminal_metric = "market.sell_1000000_terminal_price"
        for symbol in self._config.symbols:
            try:
                status = indexed[(symbol, "market.symbol_trading")]
            except KeyError as exc:
                raise ValueError(
                    f"incomplete Binance observations for {symbol}"
                ) from exc
            trading[symbol] = bool(status.value)
            times.append(status.observed_at)
            if not trading[symbol]:
                continue
            try:
                mid = indexed[(symbol, "market.mid_price")]
                exit_result = indexed[(symbol, terminal_metric)]
                fill = exit_result.metadata["fully_fillable"]
            except (KeyError, TypeError) as exc:
                raise ValueError(
                    f"incomplete Binance observations for trading symbol {symbol}"
                ) from exc
            mids[symbol] = mid.value
            terminal[symbol] = exit_result.value
            fillable[symbol] = bool(fill)
            times.extend((mid.observed_at, exit_result.observed_at))
            symbol_capacity: dict[str, dict[str, object]] = {}
            for size in self._config.sell_sizes:
                size_label = _market_size_label(size)
                metric = f"market.sell_{size_label}_terminal_price"
                capacity_observation = indexed.get((symbol, metric))
                if capacity_observation is None:
                    if require_all_exit_sizes:
                        raise ValueError(
                            f"missing Binance {size_label} exit capacity "
                            f"for {symbol}"
                        )
                    continue
                try:
                    capacity_fillable = bool(
                        capacity_observation.metadata["fully_fillable"]
                    )
                except (KeyError, TypeError) as exc:
                    raise ValueError(
                        f"incomplete Binance exit capacity for {symbol}"
                    ) from exc
                symbol_capacity[size_label] = {
                    "terminal_price": capacity_observation.value,
                    "fully_fillable": capacity_fillable,
                }
                times.append(capacity_observation.observed_at)
            exit_capacity[symbol] = symbol_capacity
        return MarketSnapshot(
            observed_at=max(times),
            mids=mids,
            trading=trading,
            one_million_terminal=terminal,
            one_million_fillable=fillable,
            exit_capacity=exit_capacity,
        )


class EvmChainMonitor:
    def __init__(
        self,
        chain: str,
        scanner: EvmScanner,
        snapshot_reader: EvmSnapshotReader,
        storage: Storage,
        *,
        watched_addresses: set[str] | None = None,
        privileged_collector: PrivilegedCallCollector | None = None,
        notifier: Notifier | None = None,
        watched_labels: tuple[str, ...] = (),
        interval_seconds: int = 30,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.chain = chain
        self._scanner = scanner
        self._snapshot_reader = snapshot_reader
        self._storage = storage
        self._watched = watched_addresses or set()
        self._privileged_collector = privileged_collector
        self._notifier = notifier
        self._watched_labels = watched_labels
        self.interval_seconds = interval_seconds

    async def check_once(self, *, deliver: bool = True) -> CheckResult:
        checked_at = datetime.now(UTC)
        try:
            scan = await self._scanner.scan_once()
            if scan.safe_head < 0:
                raise ValueError(f"{self.chain} has no confirmed safe block")
            if scan.cursor is None:
                raise ValueError(f"{self.chain} scan produced no cursor")
            processed_head = scan.cursor
            snapshot = await self._snapshot_reader.read(scan.safe_head, checked_at)
            candidate_events = list(scan.new_events)
            privileged_events: list[ChainEvent] = []
            block_hashes: dict[int, str] = {}
            if self._privileged_collector is not None and scan.start_block is not None:
                decoded = [self._to_decoded(event) for event in scan.new_events]
                privileged = await self._privileged_collector.collect(
                    scan.start_block,
                    processed_head,
                    admin=snapshot.admin,
                    owner=snapshot.owner,
                    decoded_events=decoded,
                )
                privileged_events = [
                    self._to_chain_event(event, checked_at) for event in privileged
                ]
                block_hashes = dict(
                    getattr(
                        self._privileged_collector, "last_block_hashes", {}
                    )
                )

            connection = self._storage.connection
            async with self._storage.write_lock:
                await connection.execute("BEGIN IMMEDIATE")
                try:
                    all_candidates = candidate_events + privileged_events
                    removed_events: list[ChainEvent] = []
                    reorg_from = await self._storage.block_hash_reorg_from(
                        self.chain, block_hashes
                    )
                    if reorg_from is not None:
                        removed_events = (
                            await self._storage.rollback_evm_chain_from_uncommitted(
                                self.chain, reorg_from
                            )
                        )
                    elif scan.start_block is not None:
                        removed_events = (
                            await self._storage.reconcile_chain_events_uncommitted(
                                self.chain,
                                scan.start_block,
                                processed_head,
                                all_candidates,
                                include_privileged=(
                                    self._privileged_collector is not None
                                ),
                            )
                        )
                    if removed_events and reorg_from is None:
                        await self._storage.delete_evm_observations_from_uncommitted(
                            self.chain,
                            min(event.block_number for event in removed_events),
                        )
                    previous = await self._previous_snapshot_values()
                    new_events = await self._storage.insert_chain_events_uncommitted(
                        all_candidates
                    )
                    for observation in snapshot.observations:
                        await self._storage.insert_observation_uncommitted(observation)
                    facts = self._event_facts(new_events)
                    event_causes = {
                        fact.fact_type: fact.event_key for fact in facts
                    }
                    facts.extend(
                        self._snapshot_facts(previous, snapshot, event_causes)
                    )
                    evaluations = [
                        evaluate_evm_fact(fact, self._watched) for fact in facts
                    ]
                    if removed_events or reorg_from is not None:
                        evaluations.extend(
                            await self._reorg_evaluations(
                                removed_events,
                                snapshot,
                                checked_at,
                                reorg_from=reorg_from,
                            )
                        )
                    if evaluations:
                        await StateEngine(self._storage).apply_uncommitted(
                            evaluations, checked_at
                        )
                    await self._storage.set_scan_cursor_uncommitted(
                        self.chain, processed_head
                    )
                    await self._storage.upsert_block_hashes_uncommitted(
                        self.chain, block_hashes
                    )
                    await connection.commit()
                except BaseException:
                    await connection.rollback()
                    raise
            await _record_health(
                self._storage,
                f"evm_{self.chain}",
                checked_at,
                success=True,
            )
            if deliver and self._notifier is not None:
                await _deliver_pending(self._storage, self._notifier)
            return CheckResult(
                True,
                (),
                (
                    f"evm_{self.chain} safe_head={scan.safe_head} "
                    f"cursor={scan.cursor} lag={scan.safe_head - processed_head} "
                    f"snapshot={snapshot.block_number} "
                    f"watched_labels={','.join(self._watched_labels) or '-'}",
                ),
            )
        except Exception as exc:
            logger.exception("EVM collection failed chain=%s", self.chain)
            error = f"{self.chain}: {type(exc).__name__}: {exc}"
            await _record_health(
                self._storage,
                f"evm_{self.chain}",
                checked_at,
                success=False,
                error=error,
            )
            return CheckResult(False, (error,))

    async def _reorg_evaluations(
        self,
        removed_events: list[ChainEvent],
        snapshot: EvmSnapshot,
        checked_at: datetime,
        *,
        reorg_from: int | None = None,
    ) -> list[RuleEvaluation]:
        affected_mutable: set[str] = set()
        for fact in self._event_facts(removed_events):
            evaluation = evaluate_evm_fact(fact, self._watched)
            await self._storage.cancel_pending_alerts_for_cause_uncommitted(
                fact.event_key
            )
            if evaluation.rule_id.startswith(("evm.event.", "event.evm.")):
                await self._storage.delete_risk_state_uncommitted(
                    evaluation.rule_id
                )
            else:
                affected_mutable.add(evaluation.rule_id)

        canonical_events = await self._storage.chain_events_for_chain(
            self.chain, event_types=MUTABLE_EVM_EVENT_TYPES
        )
        latest_mutable: dict[str, RuleEvaluation] = {}
        for fact in self._event_facts(canonical_events):
            evaluation = evaluate_evm_fact(fact, self._watched)
            if (
                evaluation.rule_id in affected_mutable
                and evaluation.rule_id not in latest_mutable
            ):
                latest_mutable[evaluation.rule_id] = evaluation

        pause_rule = f"evm.{self.chain}.paused"
        if snapshot.paused is not None and (
            pause_rule in affected_mutable or reorg_from is not None
        ):
            pause_fact = EvmFact(
                self.chain,
                "PAUSED" if snapshot.paused else "UNPAUSED",
                {"current": snapshot.paused, "block": snapshot.block_number},
                f"reorg-snapshot:{self.chain}:{snapshot.block_number}:paused",
            )
            latest_mutable[pause_rule] = evaluate_evm_fact(
                pause_fact, self._watched
            )

        for account, frozen in snapshot.frozen_accounts.items():
            freeze_fact = EvmFact(
                self.chain,
                "FREEZE" if frozen else "UNFREEZE",
                {
                    "account": account,
                    "current": frozen,
                    "block": snapshot.block_number,
                },
                (
                    f"reorg-snapshot:{self.chain}:{snapshot.block_number}:"
                    f"frozen:{account}"
                ),
            )
            freeze_evaluation = evaluate_evm_fact(freeze_fact, self._watched)
            if freeze_evaluation.rule_id in affected_mutable:
                latest_mutable[freeze_evaluation.rule_id] = freeze_evaluation

        first_block = (
            reorg_from
            if reorg_from is not None
            else min(event.block_number for event in removed_events)
        )
        last_block = max(
            (event.block_number for event in removed_events),
            default=snapshot.block_number,
        )
        reorg_key = f"reorg:{self.chain}:{first_block}:{last_block}"
        explorer_base = EXPLORER_BASE_URLS.get(self.chain)
        source_url = (
            f"{explorer_base}/block/{snapshot.block_number}"
            if explorer_base is not None
            else None
        )
        evaluations = list(latest_mutable.values())
        for rule_id in affected_mutable - latest_mutable.keys():
            evidence = {
                "fact_type": "REORG_CORRECTION",
                "data_time": checked_at.isoformat(),
            }
            if source_url is not None:
                evidence["source_url"] = source_url
            evaluations.append(
                RuleEvaluation(
                    rule_id,
                    RiskLevel.GREEN,
                    evidence,
                    reorg_key,
                )
            )
        reorg_evidence = {
            "fact_type": "REORG_CORRECTION",
            "removed_events": len(removed_events),
            "from_block": first_block,
            "to_block": last_block,
            "data_time": checked_at.isoformat(),
        }
        if source_url is not None:
            reorg_evidence["source_url"] = source_url
        evaluations.append(
            RuleEvaluation(
                f"event.evm.{self.chain}.{reorg_key}",
                RiskLevel.YELLOW,
                reorg_evidence,
                reorg_key,
            )
        )
        return evaluations

    async def _previous_snapshot_values(self) -> dict[str, object]:
        result: dict[str, object] = {}
        for metric in (
            "evm.implementation",
            "evm.admin",
            "evm.code_hash",
            "evm.owner",
            "evm.admin_owner",
            "evm.paused",
        ):
            observation = await self._storage.latest_observation(metric, self.chain)
            if observation is not None:
                result[metric] = observation
        return result

    def _snapshot_facts(
        self,
        previous: dict[str, object],
        snapshot: EvmSnapshot,
        event_causes: dict[str, str] | None = None,
    ) -> list[EvmFact]:
        changes: list[EvmFact] = []
        event_causes = event_causes or {}
        comparisons = (
            ("evm.implementation", "address", snapshot.implementation, "IMPLEMENTATION_CHANGED"),
            ("evm.admin", "address", snapshot.admin, "ADMIN_CHANGED"),
            ("evm.code_hash", "hash", snapshot.code_hash, "CODE_HASH_CHANGED"),
            ("evm.owner", "address", snapshot.owner, "OWNER_CHANGED"),
        )
        for metric, key, current, fact_type in comparisons:
            old = previous.get(metric)
            if old is None:
                continue
            old_value = old.metadata.get(key)  # type: ignore[union-attr]
            if old_value != current:
                event_key = f"snapshot:{snapshot.block_number}:{metric}"
                event_cause_type = (
                    "IMPLEMENTATION_CHANGED"
                    if fact_type == "CODE_HASH_CHANGED"
                    else fact_type
                )
                changes.append(
                    EvmFact(
                        self.chain,
                        fact_type,
                        {
                            "previous": old_value,
                            "current": current,
                            "block": snapshot.block_number,
                        },
                        event_key,
                        cause_id=(
                            event_causes.get(event_cause_type)
                            or (
                                f"snapshot:{self.chain}:"
                                f"{snapshot.block_number}:{metric}"
                            )
                        ),
                    )
                )
        old_admin_owner = previous.get("evm.admin_owner")
        if (
            old_admin_owner is not None
            and bool(old_admin_owner.metadata.get("supported"))  # type: ignore[union-attr]
            and snapshot.admin_owner is not None
        ):
            prior_admin_owner = old_admin_owner.metadata.get("address")  # type: ignore[union-attr]
            if prior_admin_owner != snapshot.admin_owner:
                metric = "evm.admin_owner"
                changes.append(
                    EvmFact(
                        self.chain,
                        "ADMIN_OWNER_CHANGED",
                        {
                            "previous": prior_admin_owner,
                            "current": snapshot.admin_owner,
                            "block": snapshot.block_number,
                        },
                        f"snapshot:{snapshot.block_number}:{metric}",
                        cause_id=(
                            f"snapshot:{self.chain}:"
                            f"{snapshot.block_number}:{metric}"
                        ),
                    )
                )
        old_paused = previous.get("evm.paused")
        if snapshot.paused is not None:
            prior_value = None
            supported = False
            if old_paused is not None:
                supported = bool(old_paused.metadata.get("supported"))  # type: ignore[union-attr]
                prior_value = bool(old_paused.value)  # type: ignore[union-attr]
            if (
                old_paused is None
                or not supported
                or prior_value != snapshot.paused
            ):
                changes.append(
                    EvmFact(
                        self.chain,
                        "PAUSED" if snapshot.paused else "UNPAUSED",
                        {
                            "previous": prior_value,
                            "current": snapshot.paused,
                            "block": snapshot.block_number,
                        },
                        f"snapshot:{snapshot.block_number}:paused",
                        cause_id=(
                            f"snapshot:{self.chain}:{snapshot.block_number}:paused"
                        ),
                    )
                )
        for account, frozen in snapshot.frozen_accounts.items():
            changes.append(
                EvmFact(
                    self.chain,
                    "FREEZE" if frozen else "UNFREEZE",
                    {
                        "account": account,
                        "current": frozen,
                        "block": snapshot.block_number,
                    },
                    f"snapshot:{snapshot.block_number}:frozen:{account}",
                    cause_id=(
                        f"snapshot:{self.chain}:{snapshot.block_number}:"
                        f"frozen:{account}"
                    ),
                )
            )
        return changes

    def _event_facts(self, events: list[ChainEvent]) -> list[EvmFact]:
        privileged_types = {
            "PRIVILEGED_PAUSE": "PAUSED",
            "PRIVILEGED_UNPAUSE": "UNPAUSED",
            "PRIVILEGED_TRANSFER_OWNERSHIP": "OWNER_CHANGED",
            "PRIVILEGED_CHANGE_ADMIN": "ADMIN_CHANGED",
            "PRIVILEGED_UPGRADE_TO": "IMPLEMENTATION_CHANGED",
            "PRIVILEGED_UPGRADE_TO_AND_CALL": "IMPLEMENTATION_CHANGED",
            "PRIVILEGED_PROXY_ADMIN_UPGRADE": "IMPLEMENTATION_CHANGED",
            "PRIVILEGED_PROXY_ADMIN_UPGRADE_AND_CALL": "IMPLEMENTATION_CHANGED",
        }
        relevant = {
            "MINT",
            "BURN",
            "FREEZE",
            "UNFREEZE",
            "PAUSED",
            "UNPAUSED",
            "OWNER_CHANGED",
            "IMPLEMENTATION_CHANGED",
            "ADMIN_CHANGED",
            "PRIVILEGED_UNKNOWN_CALL",
        }
        facts = []
        supply_event_representations: dict[
            tuple[object, ...], dict[str, int]
        ] = {}
        for event in events:
            fact_type = privileged_types.get(event.event_type, event.event_type)
            if event.event_type not in relevant and event.event_type not in privileged_types:
                continue
            if fact_type in {"MINT", "BURN"}:
                account = event.payload.get("account")
                if not isinstance(account, str) or not account:
                    account = event.payload.get(
                        "to_address" if fact_type == "MINT" else "from_address"
                    )
                amount = event.payload.get("amount")
                if isinstance(account, str) and isinstance(amount, (int, float)):
                    supply_key = (
                        event.tx_hash.lower(),
                        fact_type,
                        account.lower(),
                        amount,
                    )
                    representation = (
                        "custom"
                        if isinstance(event.payload.get("account"), str)
                        else "transfer"
                    )
                    counts = supply_event_representations.setdefault(
                        supply_key, {"custom": 0, "transfer": 0}
                    )
                    counts[representation] += 1
                    counterpart = (
                        "transfer" if representation == "custom" else "custom"
                    )
                    if counts[representation] <= counts[counterpart]:
                        continue
            facts.append(
                EvmFact(
                    self.chain,
                    fact_type,
                    event.payload,
                    f"{event.tx_hash}:{event.log_index}",
                )
            )
        return facts

    @staticmethod
    def _to_decoded(event: ChainEvent) -> DecodedEvent:
        return DecodedEvent(
            chain=event.chain,
            event_type=event.event_type,
            tx_hash=event.tx_hash,
            log_index=event.log_index,
            block_number=event.block_number,
            account=event.payload.get("account"),
            from_address=event.payload.get("from_address"),
            to_address=event.payload.get("to_address"),
            amount=event.payload.get("amount"),
            metadata=event.payload,
        )

    @staticmethod
    def _to_chain_event(
        event: DecodedEvent, observed_at: datetime
    ) -> ChainEvent:
        return ChainEvent(
            chain=event.chain,
            block_number=event.block_number,
            tx_hash=event.tx_hash,
            log_index=event.log_index,
            event_type=event.event_type,
            payload={
                "account": event.account,
                "from_address": event.from_address,
                "to_address": event.to_address,
                "amount": float(event.amount) if event.amount is not None else None,
                **event.metadata,
            },
            observed_at=observed_at,
        )


async def _deliver_pending(
    storage: Storage,
    notifier: Notifier,
    *,
    rate_limiter: DeliveryRateLimiter | None = None,
) -> None:
    for pending in await storage.pending_alerts():
        if rate_limiter is not None:
            slot_available, claimed = await rate_limiter.claim(pending.id)
            if not slot_available:
                break
            if not claimed:
                continue
        elif not await storage.claim_alert_delivery(pending.id):
            continue
        attempted_at = datetime.now(UTC)
        try:
            await notifier.send_text(pending.content)
        except Exception as exc:
            logger.exception("notification delivery failed alert_key=%s", pending.alert_key)
            if rate_limiter is not None:
                await rate_limiter.defer()
            await storage.record_delivery_result(
                pending.id,
                attempted_at,
                error=f"{type(exc).__name__}: {exc}",
            )
            await _record_health(
                storage,
                "notification_wechat",
                attempted_at,
                success=False,
                error=f"{type(exc).__name__}: {exc}",
            )
            break
        else:
            await storage.record_delivery_result(pending.id, attempted_at, error=None)
            await _record_health(
                storage, "notification_wechat", attempted_at, success=True
            )


class CustodyConcentrationMonitor:
    def __init__(
        self,
        collector: CustodyBalanceCollector,
        storage: Storage,
        config: CustodyConfig,
        *,
        confirmation_depths: dict[str, int] | None = None,
        evm_rpcs: dict[str, object] | None = None,
        timezone_name: str = "Asia/Shanghai",
    ) -> None:
        self._collector = collector
        self._storage = storage
        self._config = config
        self._confirmation_depths = confirmation_depths or {
            "ethereum": 0,
            "bsc": 0,
        }
        self._evm_rpcs = evm_rpcs or {}
        self._timezone_name = timezone_name

    @property
    def tick_interval_seconds(self) -> int:
        return self._config.interval_seconds

    @staticmethod
    def _address_key(chain: str, address: str) -> str:
        return address if chain == "solana" else address.casefold()

    @staticmethod
    def _custody_address_url(chain: str, address: str) -> str | None:
        explorer_base = EXPLORER_BASE_URLS.get(chain)
        if explorer_base is not None:
            return f"{explorer_base}/address/{address}"
        if chain == "solana":
            return f"https://solscan.io/account/{address}"
        return None

    async def check_once(
        self,
        *,
        deliver: bool = True,
        now: datetime | None = None,
    ) -> CheckResult:
        del deliver
        checked_at = now or datetime.now(UTC)
        if checked_at.tzinfo is None or checked_at.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        checked_at = checked_at.astimezone(UTC)
        trusted = self._config.trusted_addresses(
            current_date_for_timezone(self._timezone_name, checked_at)
        )
        trusted_keys = {
            (item.chain, self._address_key(item.chain, item.address))
            for item in trusted
        }
        binance_trusted_keys = {
            (item.chain, self._address_key(item.chain, item.address))
            for item in trusted
            if item.entity in {"binance_cex", "binance_peg_reserve"}
        }
        collections: dict[str, CustodyCollection] = {}
        errors: list[str] = []
        for chain in ("ethereum", "bsc", "solana"):
            configured = [
                item for item in self._config.addresses if item.chain == chain
            ]
            if not configured:
                continue
            effective = [item for item in trusted if item.chain == chain]
            try:
                if chain == "solana":
                    collection = await self._collector.collect_solana(
                        configured,
                        checked_at,
                        trusted_addresses=effective,
                    )
                else:
                    collection = await self._collector.collect_evm(
                        chain,
                        configured,
                        self._confirmation_depths.get(chain, 0),
                        checked_at,
                        trusted_addresses=effective,
                    )
                collections[chain] = collection
                for failure in collection.errors:
                    failure_key = (
                        failure.chain,
                        self._address_key(failure.chain, failure.address),
                    )
                    detail = (
                        f"custody: {failure.chain} {failure.label}: "
                        f"{failure.error}"
                    )
                    if failure_key in trusted_keys:
                        errors.append(detail)
                    else:
                        logger.warning(
                            "candidate custody collection failed "
                            "chain=%s address=%s label=%s error=%s",
                            failure.chain,
                            failure.address,
                            failure.label,
                            failure.error,
                        )
            except Exception as exc:
                if effective:
                    logger.exception("custody collection failed chain=%s", chain)
                    errors.append(f"custody: {chain}: {type(exc).__name__}: {exc}")
                else:
                    logger.warning(
                        "candidate-only custody collection failed chain=%s",
                        chain,
                        exc_info=True,
                    )

        invalid_evm_chains: set[str] = set()
        for chain in ("ethereum", "bsc"):
            collection = collections.get(chain)
            if collection is None or not any(item.chain == chain for item in trusted):
                continue
            cursor = await self._storage.get_scan_cursor(chain)
            if (
                cursor is not None
                and isinstance(collection.safe_block, int)
                and collection.safe_block < cursor
            ):
                invalid_evm_chains.add(chain)
                errors.append(
                    f"custody: {chain} RPC safe block {collection.safe_block} "
                    f"is behind persisted scan cursor {cursor}"
                )
        observations = [
            item
            for collection in collections.values()
            for item in collection.observations
            if not (
                item.scope.split(":", 1)[0] in invalid_evm_chains
                and item.metadata.get("status") == "trusted"
            )
        ]
        await self._persist_observations(observations)
        await self._publish_solana_deltas(
            observations, trusted_keys, checked_at
        )

        observed_trusted = {
            (
                item.scope.split(":", 1)[0],
                item.scope.split(":", 1)[1],
            )
            for item in observations
            if item.metric == "custody.address_balance"
            and item.metadata.get("status") == "trusted"
            and ":" in item.scope
        }
        trusted_complete = bool(trusted_keys) and all(
            collection.trusted_complete for collection in collections.values()
        ) and not invalid_evm_chains and trusted_keys.issubset(observed_trusted)
        if not trusted_keys:
            errors.append("custody: no effective trusted addresses")
        elif not binance_trusted_keys:
            errors.append("custody: no effective Binance trusted addresses")

        entity_totals: dict[tuple[str, str], float] = {}
        binance_total = 0.0
        source_urls: list[str] = []
        if trusted_complete:
            for item in observations:
                if item.metric != "custody.address_balance":
                    continue
                chain, address = item.scope.split(":", 1)
                if (chain, address) not in trusted_keys:
                    continue
                entity = str(item.metadata["entity"])
                entity_totals[(entity, chain)] = (
                    entity_totals.get((entity, chain), 0.0) + item.value
                )
                if entity in {"binance_cex", "binance_peg_reserve"}:
                    binance_total += item.value
                for url in item.metadata.get("evidence_urls", []):
                    if isinstance(url, str) and url not in source_urls:
                        source_urls.append(url)
            entity_rows = [
                Observation(
                    "custody.entity_balance",
                    "custody",
                    f"{entity}:{chain}",
                    value,
                    "USD1",
                    checked_at,
                    checked_at,
                    metadata={"entity": entity, "chain": chain},
                )
                for (entity, chain), value in sorted(entity_totals.items())
            ]
            await self._persist_observations(entity_rows)
        elif trusted_keys:
            errors.append("custody: trusted address set is incomplete")

        supply = await self._storage.latest_observation(
            "supply.multichain_total", "global"
        )
        supply_error = self._validate_supply(supply, checked_at)
        share: float | None = None
        concentration_evidence: dict[str, object] | None = None
        if supply_error is not None:
            errors.append(f"custody: {supply_error}")
        elif trusted_complete and binance_trusted_keys and supply is not None:
            share = binance_total / supply.value
            aggregate_rows = [
                Observation(
                    "custody.binance_verified_balance",
                    "custody",
                    "global",
                    binance_total,
                    "USD1",
                    checked_at,
                    checked_at,
                    metadata={"max_age_seconds": self._config.interval_seconds * 2},
                ),
                Observation(
                    "custody.binance_share_lower_bound",
                    "custody",
                    "global",
                    share,
                    "ratio",
                    checked_at,
                    checked_at,
                    metadata={
                        "verified_balance": binance_total,
                        "supply": supply.value,
                        "source_urls": source_urls,
                        "max_age_seconds": self._config.interval_seconds * 2,
                    },
                ),
            ]
            await self._persist_observations(aggregate_rows)
            concentration_evidence = {
                "share": share,
                "verified_balance": binance_total,
                "threshold": {
                    "yellow": self._config.yellow_share,
                    "red": self._config.red_share,
                },
                "data_time": checked_at.isoformat(),
                "source_urls": source_urls,
            }

        flow_collections = {
            chain: collection
            for chain, collection in collections.items()
            if chain not in invalid_evm_chains
        }
        flow_errors = await self._process_evm_flows(
            flow_collections,
            trusted,
            checked_at,
            evaluate_rules=not errors,
        )
        errors.extend(flow_errors)
        if errors:
            await self._interrupt_rules(
                _CUSTODY_RULE_IDS,
                checked_at,
                "; ".join(dict.fromkeys(errors)),
            )
        elif share is not None and concentration_evidence is not None:
            await self._evaluate_rule(
                "custody.binance_concentration",
                lambda previous, clear: evaluate_concentration(
                    share,
                    previous,
                    clear,
                    yellow=self._config.yellow_share,
                    red=self._config.red_share,
                    recovery_checks=self._config.recovery_checks,
                ),
                checked_at,
                concentration_evidence,
                "custody:concentration",
            )
        success = not errors
        details = ()
        if share is not None:
            details = (
                f"custody trusted_balance={binance_total:g} "
                f"share_lower_bound={share:g}",
            )
        return CheckResult(success, tuple(dict.fromkeys(errors)), details)

    @staticmethod
    def _validate_supply(
        supply: Observation | None, checked_at: datetime
    ) -> str | None:
        if supply is None:
            return "complete multichain supply is unavailable"
        if supply.quality != "FACT":
            return "complete multichain supply is not FACT"
        if not isfinite(supply.value) or supply.value <= 0:
            return "complete multichain supply is invalid"
        age = (checked_at - supply.observed_at.astimezone(UTC)).total_seconds()
        if age < 0 or age > 4500:
            return "complete multichain supply is stale"
        return None

    async def _persist_observations(
        self, observations: list[Observation]
    ) -> None:
        if not observations:
            return
        connection = self._storage.connection
        async with self._storage.write_lock:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                for item in observations:
                    await self._storage.insert_observation_uncommitted(item)
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    async def _publish_solana_deltas(
        self,
        observations: list[Observation],
        trusted_keys: set[tuple[str, str]],
        now: datetime,
    ) -> None:
        rows: list[Observation] = []
        for current in observations:
            if not current.scope.startswith("solana:"):
                continue
            chain, address = current.scope.split(":", 1)
            if (chain, address) not in trusted_keys:
                continue
            verified_on = current.metadata.get("verified_on")
            if not isinstance(verified_on, str) or not verified_on:
                continue
            for hours in (1, 24):
                boundary = now - timedelta(hours=hours)
                prior = await self._storage.nearest_fact_observation(
                    "custody.address_balance",
                    current.scope,
                    boundary,
                    max_distance_seconds=min(
                        self._config.interval_seconds * 2, 1800
                    ),
                    before_observed_at=current.observed_at,
                    metadata_equals={
                        "status": "trusted",
                        "verified_on": verified_on,
                    },
                )
                metadata: dict[str, object] = {
                    "counterparty_attribution": False,
                }
                value = 0.0
                quality = "FACT"
                if prior is None:
                    metadata["status"] = "accumulating"
                    metadata["window_hours"] = hours
                    quality = "UNAVAILABLE"
                else:
                    value = current.value - prior.value
                    metadata["status"] = "complete"
                    metadata["window_hours"] = hours
                rows.append(
                    Observation(
                        f"custody.solana_balance_delta_{hours}h",
                        "custody",
                        current.scope,
                        value,
                        "USD1",
                        now,
                        now,
                        quality=quality,
                        metadata=metadata,
                    )
                )
        await self._persist_observations(rows)

    async def _process_evm_flows(
        self,
        collections: dict[str, CustodyCollection],
        trusted: list[CustodyAddressConfig],
        now: datetime,
        *,
        evaluate_rules: bool,
    ) -> list[str]:
        errors: list[str] = []
        binance_by_chain: dict[str, set[str]] = {}
        labels: dict[str, str] = {}
        address_source_urls: dict[str, list[str]] = {}
        for item in trusted:
            if item.chain in {"ethereum", "bsc"} and item.entity in {
                "binance_cex",
                "binance_peg_reserve",
            }:
                address = item.address.casefold()
                scope = f"{item.chain}:{address}"
                binance_by_chain.setdefault(item.chain, set()).add(address)
                labels[scope] = item.label
                urls = address_source_urls.setdefault(scope, [])
                for entry in item.evidence:
                    if entry.url not in urls:
                        urls.append(entry.url)
                address_url = self._custody_address_url(item.chain, address)
                if address_url is not None and address_url not in urls:
                    urls.append(address_url)
        if not binance_by_chain:
            return errors

        covered_chains = [
            chain for chain in ("ethereum", "bsc") if chain in binance_by_chain
        ]

        all_ready_1h = True
        all_ready_24h = True
        entity_net = 0.0
        address_outflows: dict[str, float] = {}
        chain_source_urls: dict[str, str] = {}
        for chain, addresses in binance_by_chain.items():
            collection = collections.get(chain)
            if collection is None or not collection.trusted_complete:
                return [f"custody: {chain} flow balance snapshot is incomplete"]
            safe_block = collection.safe_block
            if not isinstance(safe_block, int):
                return [f"custody: {chain} flow safe block is unavailable"]
            explorer_base = EXPLORER_BASE_URLS.get(chain)
            if explorer_base is not None:
                chain_source_urls[chain] = (
                    f"{explorer_base}/block/{safe_block}"
                )
            marker = await self._storage.latest_observation(
                "custody.flow_start_block", chain
            )
            if marker is None:
                marker = Observation(
                    "custody.flow_start_block",
                    "custody",
                    chain,
                    float(safe_block),
                    "block",
                    now,
                    now,
                )
                coverage = Observation(
                    "custody.flow_coverage_start",
                    "custody",
                    chain,
                    float(safe_block),
                    "block",
                    now,
                    now,
                )
                await self._persist_observations([marker, coverage])
            coverage = await self._storage.latest_observation(
                "custody.flow_coverage_start", chain
            )
            if coverage is None:
                coverage = marker
            rpc = self._evm_rpcs.get(chain)
            if rpc is not None:
                try:
                    await enrich_transfer_timestamps(
                        chain,
                        rpc,
                        self._storage,
                        addresses,
                        min_block=int(marker.value),
                    )
                except Exception as exc:
                    errors.append(
                        f"custody: {chain} flow timestamps: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    all_ready_1h = all_ready_24h = False
                    continue
            cursor = await self._storage.get_scan_cursor(chain)
            if cursor is None or cursor < safe_block:
                all_ready_1h = all_ready_24h = False
                continue
            coverage_age = (now - coverage.observed_at.astimezone(UTC)).total_seconds()
            all_ready_1h = all_ready_1h and coverage_age >= 3600
            all_ready_24h = all_ready_24h and coverage_age >= 86400
            try:
                events = await self._storage.custody_transfers_since(
                    chain, now - timedelta(hours=24), addresses
                )
                transfers = [
                    self._transfer_from_event(event)
                    for event in events
                    if event.block_number >= int(marker.value)
                ]
                summary = summarize_transfers(
                    transfers, frozenset(addresses), now
                )
            except Exception as exc:
                errors.append(
                    f"custody: {chain} flow data: {type(exc).__name__}: {exc}"
                )
                all_ready_1h = all_ready_24h = False
                continue
            entity_net += summary.entity_net_24h
            for address, value in summary.address_outflow_1h.items():
                address_outflows[f"{chain}:{address}"] = value

        await self._publish_flow_results(
            entity_net,
            address_outflows,
            now,
            chains=covered_chains,
            labels=labels,
            address_source_urls=address_source_urls,
            chain_source_urls=chain_source_urls,
            ready_1h=all_ready_1h,
            ready_24h=all_ready_24h,
            evaluate_rules=evaluate_rules and not errors,
        )
        return errors

    @staticmethod
    def _transfer_from_event(event: ChainEvent) -> Transfer:
        raw_time = event.payload.get("block_time")
        if not isinstance(raw_time, str):
            raise ValueError("Transfer block_time is unavailable")
        return Transfer(
            str(event.payload.get("from_address", "")),
            str(event.payload.get("to_address", "")),
            float(event.payload.get("amount")),
            datetime.fromisoformat(raw_time),
        )

    async def _publish_flow_results(
        self,
        entity_net: float,
        address_outflows: dict[str, float],
        now: datetime,
        *,
        chains: list[str],
        labels: dict[str, str],
        address_source_urls: dict[str, list[str]],
        chain_source_urls: dict[str, str],
        ready_1h: bool,
        ready_24h: bool,
        evaluate_rules: bool,
    ) -> None:
        rows: list[Observation] = []
        flow_metadata = {
            "status": "complete" if ready_24h else "accumulating",
            "window_hours": 24,
        }
        rows.append(
            Observation(
                "custody.binance_net_change_24h",
                "custody",
                "global",
                entity_net if ready_24h else 0.0,
                "USD1",
                now,
                now,
                quality="FACT" if ready_24h else "UNAVAILABLE",
                metadata=flow_metadata,
            )
        )
        for scope, value in sorted(address_outflows.items()):
            rows.append(
                Observation(
                    "custody.address_external_outflow_1h",
                    "custody",
                    scope,
                    value if ready_1h else 0.0,
                    "USD1",
                    now,
                    now,
                    quality="FACT" if ready_1h else "UNAVAILABLE",
                    metadata={
                        "status": "complete" if ready_1h else "accumulating",
                        "window_hours": 1,
                    },
                )
            )
        await self._persist_observations(rows)
        interrupted: list[str] = []
        if not ready_24h:
            interrupted.append("custody.binance_flow_24h")
        if not ready_1h:
            interrupted.append("custody.address_outflow_1h")
        if interrupted:
            await self._interrupt_rules(
                tuple(interrupted), now, "custody flow window unavailable"
            )
        flow_source_urls = [
            chain_source_urls[chain]
            for chain in chains
            if chain in chain_source_urls
        ]
        outflow_source_urls: list[str] = []
        if address_outflows:
            largest_scope = max(
                address_outflows, key=lambda scope: address_outflows[scope]
            )
            outflow_source_urls.extend(
                address_source_urls.get(largest_scope, [])
            )
            largest_chain = largest_scope.partition(":")[0]
            block_url = chain_source_urls.get(largest_chain)
            if block_url is not None and block_url not in outflow_source_urls:
                outflow_source_urls.append(block_url)
        if ready_24h and evaluate_rules:
            await self._evaluate_rule(
                "custody.binance_flow_24h",
                lambda previous, clear: evaluate_entity_flow(
                    entity_net,
                    previous,
                    clear,
                    threshold=self._config.entity_flow_24h,
                    recovery_checks=self._config.recovery_checks,
                ),
                now,
                {
                    "value": entity_net,
                    "threshold": self._config.entity_flow_24h,
                    "chains": chains,
                    "label": "已核验 Binance 地址组",
                    "labels": labels,
                    "source_urls": flow_source_urls,
                    "data_time": now.isoformat(),
                },
                "custody:flow:24h",
            )
        if ready_1h and evaluate_rules:
            await self._evaluate_rule(
                "custody.address_outflow_1h",
                lambda previous, clear: evaluate_address_outflow(
                    address_outflows,
                    previous,
                    clear,
                    threshold=self._config.address_outflow_1h,
                    recovery_checks=self._config.recovery_checks,
                ),
                now,
                {
                    "values": address_outflows,
                    "threshold": self._config.address_outflow_1h,
                    "chains": chains,
                    "labels": labels,
                    "source_urls": outflow_source_urls,
                    "data_time": now.isoformat(),
                },
                "custody:flow:1h",
            )

    async def _interrupt_rules(
        self,
        rule_ids: tuple[str, ...],
        now: datetime,
        reason: str,
    ) -> None:
        await self._persist_observations(
            [
                Observation(
                    "custody.rule_interruption",
                    "custody",
                    rule_id,
                    1.0,
                    "bool",
                    now,
                    now,
                    quality="UNAVAILABLE",
                    metadata={"reason": reason},
                )
                for rule_id in rule_ids
            ]
        )

    async def record_interruption(self, now: datetime, reason: str) -> None:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        await self._interrupt_rules(
            _CUSTODY_RULE_IDS,
            now.astimezone(UTC),
            reason,
        )

    async def _evaluate_rule(
        self,
        rule_id: str,
        evaluator: Callable[[RiskLevel, int], RuleDecision],
        now: datetime,
        evidence: dict[str, object],
        cause_id: str,
    ) -> None:
        state = await self._storage.get_risk_state(rule_id)
        previous = state.level if state is not None else RiskLevel.GREEN
        prior_evidence = await self._storage.latest_observation(
            "custody.rule_state", rule_id
        )
        interruption = await self._storage.latest_observation(
            "custody.rule_interruption", rule_id
        )
        clear_checks = 0
        if (
            prior_evidence is not None
            and prior_evidence.metadata.get("level") == previous.name
            and (
                interruption is None
                or interruption.observed_at < prior_evidence.observed_at
            )
        ):
            raw_clear = prior_evidence.metadata.get("clear_checks", 0)
            if isinstance(raw_clear, int) and raw_clear >= 0:
                clear_checks = raw_clear
        decision = evaluator(previous, clear_checks)
        level = decision.level
        full_evidence = dict(evidence)
        full_evidence["clear_checks"] = decision.clear_checks
        rule_row = Observation(
            "custody.rule_state",
            "custody",
            rule_id,
            float(level),
            "risk_level",
            now,
            now,
            metadata={
                "level": level.name,
                "clear_checks": decision.clear_checks,
                "evidence": full_evidence,
            },
        )
        connection = self._storage.connection
        async with self._storage.write_lock:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                await self._storage.insert_observation_uncommitted(rule_row)
                await StateEngine(self._storage).apply_uncommitted(
                    [RuleEvaluation(rule_id, level, full_evidence, cause_id)], now
                )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise


class RedemptionChannelMonitor:
    def __init__(
        self,
        status: RedemptionStatusSource,
        pages: list[RedemptionPageSource],
        media: list[RedemptionMediaSource],
        storage: Storage,
        config: RedemptionConfig,
        notifier: Notifier | None = None,
        *,
        official_announcement_sources: tuple[str, ...] = (
            "binance",
            "wlfi",
            "occ",
        ),
    ) -> None:
        self._status = status
        self._pages = pages
        self._media = media
        self._storage = storage
        self._config = config
        self._notifier = notifier
        self._official_announcement_sources = official_announcement_sources
        self._last_runs: dict[str, datetime] = {}
        if len(pages) != len(config.official_page_urls):
            raise ValueError("redemption page collectors must match configured URLs")
        canonical_page_urls = [
            _canonical_page_url(url)[1] for url in config.official_page_urls
        ]
        self._page_scopes = {
            index: f"page:{url}"
            for index, url in enumerate(canonical_page_urls)
        }
        self._page_urls = dict(enumerate(canonical_page_urls))
        if len(set(self._page_scopes.values())) != len(self._page_scopes):
            raise ValueError("redemption page URLs must be unique")

    @property
    def tick_interval_seconds(self) -> int:
        return min(
            self._config.status_interval_seconds,
            self._config.page_interval_seconds,
        )

    @staticmethod
    def _due(
        previous: datetime | None, now: datetime, interval_seconds: int
    ) -> bool:
        return previous is None or (now - previous).total_seconds() >= interval_seconds

    async def check_once(
        self,
        *,
        deliver: bool = True,
        now: datetime | None = None,
    ) -> CheckResult:
        checked_at = now or datetime.now(UTC)
        if checked_at.tzinfo is None or checked_at.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        checked_at = checked_at.astimezone(UTC)
        errors: list[str] = []
        details: list[str] = []
        required_failed = False
        jobs: list[tuple[str, int, str, Awaitable[object]]] = []
        if self._due(
            self._last_runs.get("status"),
            checked_at,
            self._config.status_interval_seconds,
        ):
            jobs.append(("status", -1, "status", self._status.collect(checked_at)))

        for index, collector in enumerate(self._pages):
            run_key = f"page:{index}"
            if not self._due(
                self._last_runs.get(run_key),
                checked_at,
                self._config.page_interval_seconds,
            ):
                continue
            jobs.append(("page", index, run_key, collector.collect(checked_at)))

        for index, collector in enumerate(self._media):
            run_key = f"media:{index}"
            if not self._due(
                self._last_runs.get(run_key),
                checked_at,
                self._config.page_interval_seconds,
            ):
                continue
            jobs.append(("media", index, run_key, collector.collect(checked_at)))

        results = await asyncio.gather(
            *(job[3] for job in jobs), return_exceptions=True
        )
        for (kind, index, run_key, _), result in zip(jobs, results, strict=True):
            try:
                if isinstance(result, BaseException):
                    if not isinstance(result, Exception):
                        raise result
                    raise result
                if kind == "status":
                    if not isinstance(result, RedemptionStatusSnapshot):
                        raise ValueError("redemption status returned invalid result")
                    await self._persist_status_snapshot(result, checked_at)
                    detail = "redemption_status checked"
                    health_id = "redemption_status"
                elif kind == "page":
                    if not isinstance(result, Announcement):
                        raise ValueError("redemption page returned invalid result")
                    await self._persist_page(
                        result,
                        checked_at,
                        scope=self._page_scopes[index],
                        expected_url=self._page_urls[index],
                    )
                    detail = f"redemption_page_{index} checked"
                    health_id = f"redemption_page_{index}"
                else:
                    if not isinstance(result, list) or not all(
                        isinstance(item, Announcement) for item in result
                    ):
                        raise ValueError("redemption media returned invalid result")
                    await self._persist_media(result)
                    detail = f"redemption_media_{index} items={len(result)}"
                    health_id = f"redemption_media_{index}"
            except Exception as exc:
                if kind in {"status", "page"}:
                    required_failed = True
                health_id = (
                    "redemption_status"
                    if kind == "status"
                    else f"redemption_{kind}_{index}"
                )
                logger.exception("redemption collection failed source=%s", health_id)
                error = f"{health_id}: {type(exc).__name__}: {exc}"
                errors.append(error)
                await _record_health(
                    self._storage,
                    health_id,
                    checked_at,
                    success=False,
                    error=error,
                    critical=kind in {"status", "page"},
                )
            else:
                self._last_runs[run_key] = checked_at
                details.append(detail)
                await _record_health(
                    self._storage,
                    health_id,
                    checked_at,
                    success=True,
                    critical=kind in {"status", "page"},
                )

        try:
            await self._process_official_announcements(checked_at)
            aggregate_error = await self._aggregate(
                checked_at, allow_clear=not required_failed
            )
        except Exception:
            logger.exception("redemption state persistence failed")
            raise
        if aggregate_error is not None:
            errors.append(aggregate_error)

        if deliver and self._notifier is not None:
            await _deliver_pending(self._storage, self._notifier)
        return CheckResult(not errors, tuple(errors), tuple(details))

    async def _persist_status_snapshot(
        self, snapshot: RedemptionStatusSnapshot, checked_at: datetime
    ) -> None:
        source_url = snapshot.observation.metadata.get("source_url")
        observation = self._source_status_observation(
            "status:bitgo",
            RedemptionClassification(
                snapshot.level,
                snapshot.summary,
                snapshot.matched_text,
                snapshot.confirmed_usd1,
            ),
            checked_at,
            source_url=str(source_url) if isinstance(source_url, str) else "",
            max_age_seconds=self._config.status_interval_seconds * 3,
            data_time=checked_at,
            cause_key=self._cause_key(
                str(source_url) if isinstance(source_url, str) else "",
                "bitgo",
            ),
        )
        await self._storage.insert_observation(observation)

    async def _persist_page(
        self,
        item: Announcement,
        checked_at: datetime,
        *,
        scope: str,
        expected_url: str,
    ) -> None:
        if item.url != expected_url:
            raise ValueError("official redemption page returned unexpected URL")
        source_hint = item.source
        item = Announcement(
            source="redemption_page",
            stable_id=expected_url,
            title=item.title,
            url=expected_url,
            published_at=item.published_at,
            body_hash=item.body_hash,
            first_seen_at=item.first_seen_at,
            metadata=item.metadata,
        )
        previous = await self._storage.get_announcement(item.source, item.stable_id)
        previous_status = await self._storage.latest_observation(
            "redemption.source_status", scope
        )
        connection = self._storage.connection
        async with self._storage.write_lock:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                outcome = await self._storage.upsert_announcement_uncommitted(item)
                classification = self._classify_page_change(
                    outcome, previous, previous_status, item
                )
                data_time = checked_at
                prior = self._classification_from_observation(previous_status)
                if prior is not None and classification == prior:
                    assert previous_status is not None
                    data_time = self._source_data_time(previous_status)
                await self._storage.insert_observation_uncommitted(
                    self._source_status_observation(
                        scope,
                        classification,
                        checked_at,
                        source_url=item.url,
                        max_age_seconds=self._config.page_interval_seconds * 2,
                        body_hash=item.body_hash,
                        body_sections=self._sections(item),
                        data_time=data_time,
                        cause_key=self._cause_key(item.url, source_hint),
                    )
                )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    @classmethod
    def _classify_page_change(
        cls,
        outcome: str,
        previous: Announcement | None,
        previous_status: Observation | None,
        current: Announcement,
    ) -> RedemptionClassification:
        clear = cls._clear_classification()
        if outcome in {"NEW", "BASELINED"}:
            return clear
        prior = cls._classification_from_observation(previous_status) or clear
        if outcome == "UNCHANGED":
            return prior
        previous_sections = cls._sections(previous) if previous is not None else []
        additions = cls._new_sections(previous_sections, cls._sections(current))
        if not additions:
            return prior
        candidate_text = " ".join(additions)
        current_classification = classify_redemption(
            candidate_text, usd1_specific=True
        )
        if current_classification.level is not RiskLevel.GREEN:
            return current_classification
        return cls._explicit_recovery_or_prior(prior, candidate_text)

    async def _persist_media(self, items: list[Announcement]) -> None:
        connection = self._storage.connection
        async with self._storage.write_lock:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                for item in items:
                    if item.source != "media_redemption":
                        raise ValueError("redemption media item has unexpected source")
                    await self._storage.upsert_announcement_uncommitted(item)
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    async def _process_official_announcements(self, checked_at: datetime) -> None:
        async with self._storage.write_lock:
            items = await self._storage.announcements_for_sources(
                self._official_announcement_sources
            )
        for item in items:
            scope = f"announcement:{item.source}:{item.stable_id}"
            previous_status = await self._storage.latest_observation(
                "redemption.source_status", scope
            )
            if (
                previous_status is not None
                and previous_status.metadata.get("body_hash") == item.body_hash
            ):
                continue
            sections = self._sections(item)
            prior_sections = (
                self._metadata_sections(previous_status.metadata)
                if previous_status is not None
                else []
            )
            candidate_sections = self._new_sections(prior_sections, sections)
            candidate_text = " ".join(candidate_sections)
            prior = self._classification_from_observation(previous_status)
            event_time = (
                checked_at
                if previous_status is not None
                else (item.published_at or item.first_seen_at)
            )
            if not candidate_text:
                classification = prior or self._clear_classification()
            else:
                detected = classify_redemption(candidate_text, usd1_specific=True)
                if detected.level is not RiskLevel.GREEN:
                    classification = detected
                elif prior is not None:
                    classification = self._explicit_recovery_or_prior(
                        prior, candidate_text
                    )
                else:
                    classification = detected
            cause_key = self._cause_key(item.url, item.source)

            connection = self._storage.connection
            async with self._storage.write_lock:
                await connection.execute("BEGIN IMMEDIATE")
                try:
                    await self._storage.insert_observation_uncommitted(
                        self._source_status_observation(
                            scope,
                            classification,
                            checked_at,
                            source_url=item.url,
                            body_hash=item.body_hash,
                            body_sections=sections,
                            data_time=event_time,
                            cause_key=cause_key,
                        )
                    )
                    if (
                        candidate_text
                        and classification.level is RiskLevel.GREEN
                    ):
                        await self._clear_matching_confirmed_sources(
                            candidate_text,
                            item.url,
                            checked_at,
                            recovery_time=event_time,
                            except_scope=scope,
                            recovery_cause_key=cause_key,
                        )
                    await connection.commit()
                except BaseException:
                    await connection.rollback()
                    raise

    async def _clear_matching_confirmed_sources(
        self,
        recovery_text: str,
        source_url: str,
        checked_at: datetime,
        *,
        recovery_time: datetime,
        except_scope: str,
        recovery_cause_key: str,
    ) -> None:
        if recovery_time.tzinfo is None or recovery_time.utcoffset() is None:
            raise ValueError("official recovery time must be timezone-aware")
        recovery_time = recovery_time.astimezone(UTC)
        for observation in await self._storage.latest_observations_by_scope(
            "redemption.source_status"
        ):
            if observation.scope == except_scope or observation.value <= 0:
                continue
            if observation.metadata.get("cause_key") != recovery_cause_key:
                continue
            prior = self._classification_from_observation(observation)
            if (
                prior is None
                or not prior.confirmed_usd1
                or not prior.matched_text
            ):
                continue
            if self._source_data_time(observation) > recovery_time:
                continue
            combined = classify_redemption(
                f"{prior.matched_text} {recovery_text}",
                usd1_specific=True,
            )
            if combined.level is not RiskLevel.GREEN:
                continue
            await self._storage.insert_observation_uncommitted(
                self._source_status_observation(
                    observation.scope,
                    combined,
                    checked_at,
                    source_url=source_url,
                    body_hash=str(observation.metadata.get("body_hash", "")),
                    body_sections=self._metadata_sections(observation.metadata),
                    data_time=recovery_time,
                    cause_key=recovery_cause_key,
                )
            )

    async def _aggregate(
        self, checked_at: datetime, *, allow_clear: bool
    ) -> str | None:
        statuses = await self._storage.latest_observations_by_scope(
            "redemption.source_status"
        )
        by_scope = {item.scope: item for item in statuses}
        required: list[tuple[str, int]] = [
            ("status:bitgo", self._config.status_interval_seconds * 3)
        ]
        required.extend(
            (self._page_scopes[index], self._config.page_interval_seconds * 2)
            for index in range(len(self._pages))
        )
        remaining_validity: list[float] = []
        for scope, max_age in required:
            item = by_scope.get(scope)
            if item is None:
                return f"redemption: required source unavailable: {scope}"
            try:
                age = _validated_observation_age(item.observed_at, checked_at)
            except ValueError as exc:
                return f"redemption: invalid source time for {scope}: {exc}"
            if age >= max_age:
                return f"redemption: required source stale: {scope}"
            remaining_validity.append(max_age - age)
        aggregate_max_age = int(min(remaining_validity))
        if aggregate_max_age <= 0:
            return "redemption: required source validity is too short"

        active_scopes = {scope for scope, _ in required}
        active_scopes.update(
            item.scope
            for item in statuses
            if any(
                item.scope.startswith(f"announcement:{source}:")
                for source in self._official_announcement_sources
            )
        )
        active = [item for item in statuses if item.scope in active_scopes]
        if not active:
            return "redemption: no source status is available"
        winner = max(
            active,
            key=lambda item: (
                item.value,
                bool(item.metadata.get("confirmed_usd1")),
                item.observed_at,
                item.scope,
            ),
        )
        try:
            raw_level = RiskLevel(int(winner.value))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"invalid redemption source level for {winner.scope}"
            ) from exc

        previous_state = await self._storage.get_risk_state("redemption.channel")
        previous_level = (
            previous_state.level if previous_state is not None else RiskLevel.GREEN
        )
        previous_aggregate = await self._storage.latest_observation(
            "redemption.channel_status", "global"
        )
        previous_clear_checks = 0
        if previous_aggregate is not None:
            value = previous_aggregate.metadata.get("clear_checks", 0)
            if type(value) is int and value >= 0:
                previous_clear_checks = value
        if (
            not allow_clear
            and raw_level is RiskLevel.GREEN
            and previous_level is not RiskLevel.GREEN
        ):
            effective_level = previous_level
            clear_checks = 0
        else:
            recovery = apply_recovery(
                previous_level,
                raw_level,
                previous_clear_checks,
                required=self._config.recovery_checks,
            )
            effective_level = recovery.level
            clear_checks = recovery.clear_checks
        evidence_source = winner
        if effective_level > raw_level and previous_aggregate is not None:
            evidence_metadata = dict(previous_aggregate.metadata)
        else:
            evidence_metadata = dict(evidence_source.metadata)
        summary = str(evidence_metadata.get("summary", "未发现官方限制"))
        source_url = str(evidence_metadata.get("source_url", ""))
        matched_text = evidence_metadata.get("matched_text")
        confirmed_usd1 = bool(evidence_metadata.get("confirmed_usd1", False))
        data_time = str(
            evidence_metadata.get("data_time", evidence_source.observed_at.isoformat())
        )
        source_scope = str(
            evidence_metadata.get("source_scope", evidence_source.scope)
        )
        cause_key = str(evidence_metadata.get("cause_key", ""))
        metadata = {
            "summary": summary,
            "matched_text": matched_text,
            "confirmed_usd1": confirmed_usd1,
            "source_url": source_url,
            "data_time": data_time,
            "source_scope": source_scope,
            "cause_key": cause_key,
            "clear_checks": clear_checks,
            "max_age_seconds": aggregate_max_age,
        }
        aggregate = Observation(
            "redemption.channel_status",
            "redemption",
            "global",
            float(effective_level),
            "risk_level",
            checked_at,
            checked_at,
            metadata=metadata,
        )
        evidence = {
            "summary": summary,
            "matched_text": matched_text,
            "confirmed_usd1": confirmed_usd1,
            "source_url": source_url,
            "data_time": data_time,
            "cause_key": cause_key,
        }
        connection = self._storage.connection
        async with self._storage.write_lock:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                await self._storage.insert_observation_uncommitted(aggregate)
                await StateEngine(self._storage).apply_uncommitted(
                    [
                        RuleEvaluation(
                            "redemption.channel",
                            effective_level,
                            evidence,
                            f"redemption:{cause_key}" if cause_key else "redemption:channel",
                        )
                    ],
                    checked_at,
                )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        return None

    @staticmethod
    def _clear_classification() -> RedemptionClassification:
        return RedemptionClassification(
            RiskLevel.GREEN, "未发现官方限制", None, False
        )

    @staticmethod
    def _sections(item: Announcement | None) -> list[str]:
        if item is None:
            return []
        raw = item.metadata.get("body_sections")
        values = raw if isinstance(raw, list) else [item.metadata.get("body_text", "")]
        sections = [
            normalized
            for value in values
            if isinstance(value, str) and (normalized := normalize_text(value))
        ]
        title = normalize_text(item.title)
        if title and title not in sections:
            sections.insert(0, title)
        return sections

    @staticmethod
    def _metadata_sections(metadata: dict[str, object]) -> list[str]:
        raw = metadata.get("body_sections")
        if not isinstance(raw, list):
            return []
        return [
            normalized
            for value in raw
            if isinstance(value, str) and (normalized := normalize_text(value))
        ]

    @staticmethod
    def _new_sections(previous: list[str], current: list[str]) -> list[str]:
        previous_set = {normalize_text(value) for value in previous}
        return [value for value in current if normalize_text(value) not in previous_set]

    @staticmethod
    def _classification_from_observation(
        observation: Observation | None,
    ) -> RedemptionClassification | None:
        if observation is None:
            return None
        try:
            level = RiskLevel(int(observation.value))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"invalid redemption source status for {observation.scope}"
            ) from exc
        raw_matched = observation.metadata.get("matched_text")
        return RedemptionClassification(
            level,
            str(observation.metadata.get("summary", "未发现官方限制")),
            raw_matched if isinstance(raw_matched, str) and raw_matched else None,
            bool(observation.metadata.get("confirmed_usd1", False)),
        )

    @staticmethod
    def _explicit_recovery_or_prior(
        prior: RedemptionClassification, candidate_text: str
    ) -> RedemptionClassification:
        if prior.level is RiskLevel.GREEN or not prior.matched_text:
            return prior
        combined = classify_redemption(
            f"{prior.matched_text} {candidate_text}", usd1_specific=True
        )
        return combined if combined.level is RiskLevel.GREEN else prior

    @staticmethod
    def _source_data_time(observation: Observation) -> datetime:
        raw = observation.metadata.get("data_time")
        if not isinstance(raw, str):
            return observation.observed_at.astimezone(UTC)
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError(
                f"invalid redemption source data_time for {observation.scope}"
            ) from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(
                f"invalid redemption source data_time for {observation.scope}"
            )
        return parsed.astimezone(UTC)

    @staticmethod
    def _source_status_observation(
        scope: str,
        classification: RedemptionClassification,
        checked_at: datetime,
        *,
        source_url: str,
        max_age_seconds: int | None = None,
        body_hash: str = "",
        body_sections: list[str] | None = None,
        data_time: datetime | None = None,
        cause_key: str,
    ) -> Observation:
        effective_data_time = data_time or checked_at
        if (
            effective_data_time.tzinfo is None
            or effective_data_time.utcoffset() is None
        ):
            raise ValueError("redemption source data_time must be timezone-aware")
        metadata: dict[str, object] = {
            "summary": classification.summary,
            "matched_text": classification.matched_text,
            "confirmed_usd1": classification.confirmed_usd1,
            "source_url": source_url,
            "body_hash": body_hash,
            "body_sections": body_sections or [],
            "data_time": effective_data_time.astimezone(UTC).isoformat(),
            "cause_key": cause_key,
        }
        if max_age_seconds is not None:
            metadata["max_age_seconds"] = max_age_seconds
        return Observation(
            "redemption.source_status",
            "redemption",
            scope,
            float(classification.level),
            "risk_level",
            checked_at,
            checked_at,
            metadata=metadata,
        )

    @staticmethod
    def _cause_key(source_url: str, source_hint: str) -> str:
        hostname = (urlsplit(source_url).hostname or "").casefold()
        authorities = (
            ("bitgo.com", "bitgo"),
            ("worldlibertyfinancial.com", "wlfi"),
            ("binance.com", "binance"),
            ("occ.gov", "occ"),
        )
        for suffix, authority in authorities:
            if hostname == suffix or hostname.endswith(f".{suffix}"):
                return f"{authority}:usd1_redemption"
        normalized_hint = source_hint.casefold()
        for _, authority in authorities:
            if authority in normalized_hint:
                return f"{authority}:usd1_redemption"
        return f"{normalized_hint}:usd1_redemption"


class Usd1Monitor:
    def __init__(
        self,
        market: MarketMonitor,
        evm_chains: list[EvmChainMonitor],
        storage: Storage,
        notifier: Notifier | None,
        *,
        interval_seconds: int = 60,
        reserve_supply: "ReserveSupplyMonitor | None" = None,
        information: "InformationMonitor | None" = None,
        custody: "CustodyConcentrationMonitor | None" = None,
        redemption: "RedemptionChannelMonitor | None" = None,
        retention_days: int = 180,
        check_timeout_seconds: float = 45.0,
    ) -> None:
        if check_timeout_seconds <= 0:
            raise ValueError("check_timeout_seconds must be positive")
        self._market = market
        self._evm_chains = evm_chains
        self._storage = storage
        self._notifier = notifier
        self._interval_seconds = interval_seconds
        self._startup_sent = False
        self._reserve_supply = reserve_supply
        self._information = information
        self._custody = custody
        self._redemption = redemption
        self._retention_days = retention_days
        self._check_timeout_seconds = check_timeout_seconds
        self._last_prune: datetime | None = None
        self._stop = asyncio.Event()
        self._delivery_lock = asyncio.Lock()
        self._delivery_rate_limiter = DeliveryRateLimiter(storage)
        self._maintenance_lock = asyncio.Lock()

    def stop(self) -> None:
        self._stop.set()

    async def check_once(self, *, deliver: bool = True) -> CheckResult:
        checks = [
            self._check_component_once(
                "market", lambda: self._market.check_once(deliver=False)
            )
        ]
        checks.extend(
            self._check_component_once(
                f"evm_{chain.chain}",
                lambda chain=chain: chain.check_once(deliver=False),
            )
            for chain in self._evm_chains
        )
        if self._reserve_supply is not None:
            checks.append(
                self._check_component_once(
                    "reserve_supply",
                    lambda: self._reserve_supply.check_once(deliver=False),
                )
            )
        if self._information is not None:
            checks.append(
                self._check_component_once(
                    "information",
                    lambda: self._information.check_once(deliver=False),
                )
            )
        if self._custody is not None:
            checks.append(
                self._check_component_once(
                    "custody",
                    lambda: self._custody.check_once(deliver=False),
                )
            )
        if self._redemption is not None:
            checks.append(
                self._check_component_once(
                    "redemption",
                    lambda: self._redemption.check_once(deliver=False),
                )
            )
        results = list(await asyncio.gather(*checks))
        await self._prune_if_due()
        if deliver and self._notifier is not None:
            async with self._delivery_lock:
                await _deliver_pending(
                    self._storage,
                    self._notifier,
                    rate_limiter=self._delivery_rate_limiter,
                )
        errors = tuple(error for result in results for error in result.errors)
        details = tuple(detail for result in results for detail in result.details)
        return CheckResult(not errors, errors, details)

    async def _check_component_once(
        self,
        name: str,
        check: Callable[[], Awaitable[CheckResult]],
    ) -> CheckResult:
        try:
            if name.startswith("evm_"):
                # Cancelling a bounded catch-up cycle discards its cursor progress.
                result = await check()
            else:
                result = await asyncio.wait_for(
                    check(), timeout=self._check_timeout_seconds
                )
        except TimeoutError:
            error = (
                f"{name}: TimeoutError: exceeded "
                f"{self._check_timeout_seconds:g}s"
            )
            logger.error("monitor component timed out component=%s", name)
            checked_at = datetime.now(UTC)
            errors = (
                await self._record_custody_interruption(checked_at, error)
                if name == "custody"
                else (error,)
            )
            await _record_health(
                self._storage,
                f"scheduler_{name}",
                checked_at,
                success=False,
                error="; ".join(errors),
                critical=True,
            )
            return CheckResult(False, errors)
        except Exception as exc:
            if name != "custody":
                raise
            error = f"custody: {type(exc).__name__}: {exc}"
            logger.exception("monitor component failed component=custody")
            checked_at = datetime.now(UTC)
            errors = await self._record_custody_interruption(checked_at, error)
            await _record_health(
                self._storage,
                "scheduler_custody",
                checked_at,
                success=False,
                error="; ".join(errors),
                critical=True,
            )
            return CheckResult(False, errors)
        await _record_health(
            self._storage,
            f"scheduler_{name}",
            datetime.now(UTC),
            success=result.success if name == "custody" else True,
            error=("; ".join(result.errors) if not result.success else None),
            critical=True,
        )
        return result

    async def _record_custody_interruption(
        self, checked_at: datetime, original_error: str
    ) -> tuple[str, ...]:
        if self._custody is None:
            return (original_error,)
        try:
            await self._custody.record_interruption(checked_at, original_error)
        except Exception as exc:
            logger.exception(
                "custody interruption recording failed original_error=%s",
                original_error,
            )
            return (
                original_error,
                "custody interruption recording failed: "
                f"{type(exc).__name__}: {exc}",
            )
        return (original_error,)

    async def _prune_if_due(self) -> None:
        async with self._maintenance_lock:
            now = datetime.now(UTC)
            expired = await self._storage.expired_event_risk_states(
                now - timedelta(seconds=self._storage.event_active_seconds)
            )
            if expired:
                def expiry_evaluations(states):
                    return [
                        RuleEvaluation(
                            state.rule_id,
                            RiskLevel.GREEN,
                            {
                                "current": "event window expired",
                                "threshold": self._storage.event_active_seconds,
                                "data_time": now.isoformat(),
                            },
                            f"expiry:{state.rule_id}",
                        )
                        for state in states
                    ]

                information_expired = [
                    state
                    for state in expired
                    if state.rule_id.startswith("event.information.")
                ]
                other_expired = [
                    state
                    for state in expired
                    if not state.rule_id.startswith("event.information.")
                ]
                engine = StateEngine(self._storage)
                if information_expired:
                    await engine.apply(
                        expiry_evaluations(information_expired),
                        now,
                        enqueue_alerts=False,
                    )
                if other_expired:
                    await engine.apply(
                        expiry_evaluations(other_expired),
                        now,
                    )
            if (
                self._last_prune is not None
                and (now - self._last_prune).total_seconds() < 86400
            ):
                return
            await self._storage.prune_observations(
                now - timedelta(days=self._retention_days)
            )
            await self._storage.prune_transient_risk_states(
                now - timedelta(days=self._retention_days)
            )
            self._last_prune = now

    def _component_checks(
        self,
    ) -> list[tuple[str, Callable[[], Awaitable[CheckResult]], int]]:
        checks: list[
            tuple[str, Callable[[], Awaitable[CheckResult]], int]
        ] = [
            (
                "market",
                lambda: self._market.check_once(deliver=False),
                self._interval_seconds,
            )
        ]
        checks.extend(
            (
                f"evm_{chain.chain}",
                lambda chain=chain: chain.check_once(deliver=False),
                getattr(chain, "interval_seconds", self._interval_seconds),
            )
            for chain in self._evm_chains
        )
        if self._reserve_supply is not None:
            checks.append(
                (
                    "reserve_supply",
                    lambda: self._reserve_supply.check_once(deliver=False),
                    self._reserve_supply.tick_interval_seconds,
                )
            )
        if self._information is not None:
            checks.append(
                (
                    "information",
                    lambda: self._information.check_once(deliver=False),
                    self._information.tick_interval_seconds,
                )
            )
        if self._custody is not None:
            checks.append(
                (
                    "custody",
                    lambda: self._custody.check_once(deliver=False),
                    self._custody.tick_interval_seconds,
                )
            )
        if self._redemption is not None:
            checks.append(
                (
                    "redemption",
                    lambda: self._redemption.check_once(deliver=False),
                    self._redemption.tick_interval_seconds,
                )
            )
        return checks

    async def _run_component(
        self,
        name: str,
        check: Callable[[], Awaitable[CheckResult]],
        interval_seconds: int,
    ) -> None:
        loop = asyncio.get_running_loop()
        next_tick = loop.time()
        while not self._stop.is_set():
            try:
                await self._check_component_once(name, check)
                await self._prune_if_due()
                if self._notifier is not None:
                    async with self._delivery_lock:
                        await _deliver_pending(
                            self._storage,
                            self._notifier,
                            rate_limiter=self._delivery_rate_limiter,
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(
                    "monitor component iteration failed component=%s", name
                )
                try:
                    await _record_health(
                        self._storage,
                        f"scheduler_{name}",
                        datetime.now(UTC),
                        success=False,
                        error=f"{type(exc).__name__}: {exc}",
                        critical=True,
                    )
                except Exception:
                    logger.exception(
                        "failed to persist scheduler health component=%s", name
                    )
            next_tick += interval_seconds
            timeout = max(0.0, next_tick - loop.time())
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=timeout)
            except TimeoutError:
                pass

    async def send_startup_once(self) -> None:
        if self._startup_sent or self._notifier is None:
            return
        if not await self._delivery_rate_limiter.try_acquire():
            return
        monitored = ["价格", "流动性", "交易状态"]
        monitored.extend(
            f"{CHAIN_LABELS.get(item.chain, item.chain)} 合约权限"
            for item in self._evm_chains
        )
        if self._reserve_supply is not None:
            monitored.append("储备与供应量")
            monitored.append("储备覆盖率")
            monitored.append("完整多链供应量与桥接核对")
        if self._information is not None:
            monitored.append("官方公告")
        if self._custody is not None:
            monitored.append("已核验 Binance 地址集中度与资金流")
        if self._redemption is not None:
            monitored.append("官方赎回通道")
        try:
            await self._notifier.send_text(
                format_startup_message(monitored, NOT_MONITORED)
            )
        except Exception as exc:
            await self._delivery_rate_limiter.defer()
            logger.exception("startup notification failed")
            await _record_health(
                self._storage,
                "notification_wechat",
                datetime.now(UTC),
                success=False,
                error=f"{type(exc).__name__}: {exc}",
            )
            return
        self._startup_sent = True

    async def run(self) -> None:
        if self._notifier is None:
            logger.warning("notifications are disabled: WECHAT_WEBHOOK is not configured")
        await self.send_startup_once()
        loop = asyncio.get_running_loop()
        installed_signals: list[signal.Signals] = []
        for name in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(name, self.stop)
                installed_signals.append(name)
            except (NotImplementedError, RuntimeError):
                pass
        tasks = [
            asyncio.create_task(
                self._run_component(name, check, interval_seconds), name=name
            )
            for name, check, interval_seconds in self._component_checks()
        ]
        stop_task = asyncio.create_task(self._stop.wait(), name="monitor_stop")
        try:
            done, _ = await asyncio.wait(
                [*tasks, stop_task], return_when=asyncio.FIRST_COMPLETED
            )
            if stop_task not in done:
                await next(iter(done))
        finally:
            stop_task.cancel()
            for task in tasks:
                task.cancel()
            await asyncio.gather(stop_task, *tasks, return_exceptions=True)
            for name in installed_signals:
                loop.remove_signal_handler(name)


class ConfirmedPorSource:
    def __init__(
        self, rpc: object, collector: PorCollector, confirmation_depth: int
    ) -> None:
        self._rpc = rpc
        self._collector = collector
        self._confirmation_depth = confirmation_depth

    async def collect(self, collected_at: datetime) -> PorSnapshot:
        latest = int(await self._rpc.call("eth_blockNumber", []), 16)  # type: ignore[attr-defined]
        safe_block = latest - self._confirmation_depth
        if safe_block < 0:
            raise ValueError("Ethereum has no confirmed block for PoR")
        return await self._collector.collect(safe_block, collected_at)


@dataclass(frozen=True)
class SupplyBatch:
    snapshots: tuple[SupplySnapshot, ...]
    errors: tuple[tuple[str, Exception], ...] = ()
    multichain_complete: bool = False


class CombinedSupplySource:
    def __init__(
        self,
        multichain_source: MultichainSupplySource,
        global_source: DefiLlamaSupplyCollector,
    ) -> None:
        self._multichain = multichain_source
        self._global = global_source

    async def collect(self, collected_at: datetime) -> SupplyBatch:
        multichain_result, global_result = await asyncio.gather(
            self._multichain.collect(collected_at),
            self._global.collect(collected_at),
            return_exceptions=True,
        )
        snapshots: list[SupplySnapshot] = []
        errors: list[tuple[str, Exception]] = []
        multichain_complete = False
        if isinstance(multichain_result, Exception):
            errors.append(("multichain", multichain_result))
        elif isinstance(multichain_result, BaseException):
            raise multichain_result
        else:
            snapshots.extend(multichain_result.components)
            snapshots.extend(multichain_result.totals)
            errors.extend(multichain_result.errors)
            multichain_complete = multichain_result.complete
        if isinstance(global_result, Exception):
            errors.append(("defillama", global_result))
        elif isinstance(global_result, BaseException):
            raise global_result
        else:
            snapshots.append(global_result)
        return SupplyBatch(
            tuple(snapshots),
            tuple(errors),
            multichain_complete,
        )


class PorSource(Protocol):
    async def collect(self, collected_at: datetime) -> PorSnapshot: ...


class SupplySource(Protocol):
    async def collect(
        self, collected_at: datetime
    ) -> list[SupplySnapshot] | SupplyBatch: ...


class ReserveSupplyMonitor:
    def __init__(
        self,
        por_collector: PorSource,
        supply_collector: SupplySource,
        storage: Storage,
        notifier: Notifier | None,
        *,
        por_config: PorConfig | None = None,
        supply_config: SupplyConfig | None = None,
    ) -> None:
        self._por = por_collector
        self._supply = supply_collector
        self._storage = storage
        self._notifier = notifier
        self._por_config = por_config or PorConfig()
        self._supply_config = supply_config or SupplyConfig()
        self._last_por_run: datetime | None = None
        self._last_supply_run: datetime | None = None

    @property
    def tick_interval_seconds(self) -> int:
        return min(
            self._por_config.interval_seconds,
            self._supply_config.interval_seconds,
        )

    async def check_once(
        self,
        *,
        deliver: bool = True,
        now: datetime | None = None,
    ) -> CheckResult:
        checked_at = now or datetime.now(UTC)
        if self._last_supply_run is None:
            last_supply = await self._storage.latest_observation(
                "supply.multichain_total", "global"
            )
            if last_supply is not None:
                self._last_supply_run = last_supply.collected_at
        por_due = self._is_due(
            self._last_por_run, checked_at, self._por_config.interval_seconds
        )
        supply_due = self._is_due(
            self._last_supply_run,
            checked_at,
            self._supply_config.interval_seconds,
        )
        if por_due and supply_due:
            collected = await asyncio.gather(
                self._por.collect(checked_at),
                self._supply.collect(checked_at),
                return_exceptions=True,
            )
            por_result = await self._check_por(
                checked_at,
                collected=collected[0],
            )
            supply_result = await self._check_supply(
                checked_at,
                collected=collected[1],
            )
            results = [por_result, supply_result]
        else:
            checks: list[Awaitable[CheckResult]] = []
            if por_due:
                checks.append(self._check_por(checked_at))
            if supply_due:
                checks.append(self._check_supply(checked_at))
            results = list(await asyncio.gather(*checks)) if checks else []
        if deliver and self._notifier is not None:
            await _deliver_pending(self._storage, self._notifier)
        errors = tuple(error for result in results for error in result.errors)
        details = tuple(detail for result in results for detail in result.details)
        return CheckResult(not errors, errors, details)

    async def _check_por(
        self,
        checked_at: datetime,
        *,
        collected: PorSnapshot | BaseException | object = _NOT_COLLECTED,
    ) -> CheckResult:
        try:
            if collected is _NOT_COLLECTED:
                por = await self._por.collect(checked_at)
            elif isinstance(collected, BaseException):
                raise collected
            else:
                por = collected
            _validated_observation_age(por.observed_at, checked_at)
            await self._persist_por(por, checked_at)
            await _record_health(
                self._storage, "por", checked_at, success=True, critical=True
            )
            self._last_por_run = checked_at
            detail = (
                f"por reserves={por.reserves} data_time={por.observed_at.isoformat()} "
                f"age={checked_at.timestamp() - por.oracle_timestamp:.0f}s"
            )
            return CheckResult(True, (), (detail,))
        except Exception as exc:
            logger.exception("PoR collection failed")
            error = f"por/oracle: {type(exc).__name__}: {exc}"
            await _record_health(
                self._storage,
                "por",
                checked_at,
                success=False,
                error=error,
                critical=True,
            )
            return CheckResult(False, (error,))

    async def _check_supply(
        self,
        checked_at: datetime,
        *,
        collected: (
            tuple[SupplySnapshot, ...] | SupplyBatch | BaseException | object
        ) = _NOT_COLLECTED,
    ) -> CheckResult:
        try:
            if collected is _NOT_COLLECTED:
                collected = await self._supply.collect(checked_at)
            elif isinstance(collected, BaseException):
                raise collected
            batch = (
                collected
                if isinstance(collected, SupplyBatch)
                else SupplyBatch(tuple(collected))
            )
            valid_supplies: list[SupplySnapshot] = []
            batch_errors = list(batch.errors)
            native_max_age = self._supply_config.interval_seconds + 900
            for supply in batch.snapshots:
                if (
                    supply.observation.metric == "supply.native"
                    and (
                        checked_at - supply.observed_at
                    ).total_seconds() > native_max_age
                ):
                    batch_errors.append(
                        (
                            str(
                                supply.observation.metadata.get(
                                    "component_id", supply.scope
                                )
                            ),
                            SupplyDataError(
                                f"safe block is older than {native_max_age}s"
                            ),
                        )
                    )
                    continue
                valid_supplies.append(supply)
            batch = SupplyBatch(
                tuple(valid_supplies),
                tuple(batch_errors),
                batch.multichain_complete
                and len(valid_supplies) == len(batch.snapshots),
            )
            supplies = batch.snapshots
            await self._persist_supplies(batch, checked_at)
            details: list[str] = []
            aggregate_values: dict[str, float] = {}
            for supply in supplies:
                metric = supply.observation.metric
                if metric == "supply.native":
                    details.append(
                        f"supply_native {supply.scope}={supply.supply:g}"
                    )
                elif metric == "supply.bridged":
                    details.append(
                        f"supply_bridged {supply.scope}={supply.supply:g}"
                    )
                elif metric == "bridge.locked":
                    details.append(
                        f"bridge_locked {supply.scope}={supply.supply:g}"
                    )
                elif metric in {
                    "supply.multichain_total",
                    "supply.bridged_total",
                    "bridge.locked_total",
                    "bridge.issuance_delta",
                }:
                    aggregate_values[metric] = supply.supply
                elif metric == "supply.global":
                    details.append(
                        f"supply_defillama global={supply.supply:g}"
                    )
            if "supply.multichain_total" in aggregate_values:
                details.append(
                    "supply_multichain total="
                    f"{aggregate_values['supply.multichain_total']:g}"
                )
            if all(
                metric in aggregate_values
                for metric in (
                    "supply.bridged_total",
                    "bridge.locked_total",
                    "bridge.issuance_delta",
                )
            ):
                details.append(
                    "bridge_reconciliation "
                    f"issued={aggregate_values['supply.bridged_total']:g} "
                    f"locked={aggregate_values['bridge.locked_total']:g} "
                    f"delta={aggregate_values['bridge.issuance_delta']:g}"
                )

            errors_by_id = {
                source: f"supply_{source}: {type(exc).__name__}: {exc}"
                for source, exc in batch.errors
            }
            observed_component_ids = {
                str(component_id)
                for supply in supplies
                if (
                    component_id := supply.observation.metadata.get(
                        "component_id"
                    )
                )
            }
            required_failed_ids = (
                REQUIRED_COMPONENT_IDS - observed_component_ids
            ) | (set(errors_by_id) & REQUIRED_COMPONENT_IDS)
            for component_id in sorted(REQUIRED_COMPONENT_IDS):
                error = errors_by_id.get(component_id)
                if component_id in required_failed_ids and error is None:
                    error = f"supply_{component_id}: component missing"
                await _record_health(
                    self._storage,
                    f"supply_{component_id}",
                    checked_at,
                    success=component_id not in required_failed_ids,
                    error=error,
                    enqueue_alerts=False,
                )

            required_errors = [
                errors_by_id.get(
                    component_id,
                    f"supply_{component_id}: component missing",
                )
                for component_id in sorted(required_failed_ids)
            ]
            await _record_health(
                self._storage,
                "supply_multichain",
                checked_at,
                success=not required_failed_ids,
                error="; ".join(required_errors) or None,
                extra_evidence={
                    "failed_sources": sorted(required_failed_ids)
                },
            )

            has_defillama = any(
                supply.observation.source == "defillama"
                and supply.observation.metric == "supply.global"
                for supply in supplies
            )
            if has_defillama or "defillama" in errors_by_id:
                await _record_health(
                    self._storage,
                    "supply_defillama",
                    checked_at,
                    success=has_defillama,
                    error=errors_by_id.get("defillama"),
                )

            for source, error in errors_by_id.items():
                if source in REQUIRED_COMPONENT_IDS or source == "defillama":
                    continue
                await _record_health(
                    self._storage,
                    f"supply_{source}",
                    checked_at,
                    success=False,
                    error=error,
                    enqueue_alerts=False,
                )

            await _record_health(
                self._storage,
                "supply",
                checked_at,
                success=not required_failed_ids,
                error="; ".join(required_errors) or None,
                enqueue_alerts=False,
            )
            errors = [
                error
                for source, error in errors_by_id.items()
                if source != "defillama" or required_failed_ids
            ]
            self._last_supply_run = checked_at
            return CheckResult(not errors, tuple(errors), tuple(details))
        except Exception as exc:
            logger.exception("supply collection failed")
            error = f"supply: {type(exc).__name__}: {exc}"
            await _record_health(
                self._storage,
                "supply_multichain",
                checked_at,
                success=False,
                error=error,
                extra_evidence={
                    "failed_sources": sorted(REQUIRED_COMPONENT_IDS)
                },
            )
            return CheckResult(False, (error,))

    @staticmethod
    def _is_due(
        previous: datetime | None, now: datetime, interval_seconds: int
    ) -> bool:
        return previous is None or (now - previous).total_seconds() >= interval_seconds

    async def _evaluate_por(self, now: datetime) -> None:
        evaluations = await self._por_evaluations(now)
        if evaluations:
            await StateEngine(self._storage).apply(evaluations, now)

    async def _persist_por(
        self,
        por: PorSnapshot,
        now: datetime,
    ) -> None:
        connection = self._storage.connection
        async with self._storage.write_lock:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                for observation in por.observations:
                    await self._storage.insert_observation_uncommitted(observation)
                evaluations = await self._por_evaluations(now)
                if evaluations:
                    await StateEngine(self._storage).apply_uncommitted(
                        evaluations, now
                    )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    async def _por_evaluations(self, now: datetime) -> list[RuleEvaluation]:
        rows = await self._storage.latest_observations("por.reserves", limit=20)
        readings = [
            PorReading(item.value, item.observed_at, item.collected_at)
            for item in reversed(rows)
        ]
        result = evaluate_por(
            readings,
            now,
            yellow_seconds=self._por_config.yellow_staleness_seconds,
            red_seconds=self._por_config.red_staleness_seconds,
            change_threshold=self._por_config.relative_change_threshold,
            recovery_read_count=self._por_config.recovery_read_count,
        )
        distinct_collections = {item.observed_at: item for item in readings}
        recent_collections = sorted(
            distinct_collections.values(),
            key=lambda item: item.observed_at,
        )[-self._por_config.recovery_read_count:]
        stable_recovery = (
            result.valid_recovery
            and len(recent_collections) == self._por_config.recovery_read_count
            and all(
                evaluate_reserve_change(
                    previous.reserves,
                    current.reserves,
                    threshold=self._por_config.relative_change_threshold,
                )
                is RiskLevel.GREEN
                for previous, current in zip(
                    recent_collections, recent_collections[1:]
                )
            )
        )
        evaluations: list[RuleEvaluation] = []
        source_urls = _observation_source_urls(rows[0])
        if isinstance(result.age_level, RiskLevel):
            evidence = {
                "current": (now - readings[-1].observed_at).total_seconds(),
                "threshold": self._por_config.yellow_staleness_seconds,
                "data_time": readings[-1].observed_at.isoformat(),
            }
            if source_urls:
                evidence["source_urls"] = source_urls
            evaluations.append(
                RuleEvaluation(
                    "por.age",
                    result.age_level,
                    evidence,
                )
            )
        if isinstance(result.change_level, RiskLevel):
            prior_change = await self._storage.get_risk_state(
                "por.reserve_change"
            )
            change_level = result.change_level
            if prior_change is not None and prior_change.level is not RiskLevel.GREEN:
                change_level = (
                    RiskLevel.GREEN
                    if stable_recovery
                    else prior_change.level
                )
            evidence = {
                "current": readings[-1].reserves,
                "threshold": self._por_config.relative_change_threshold,
                "data_time": readings[-1].observed_at.isoformat(),
            }
            if source_urls:
                evidence["source_urls"] = source_urls
            evaluations.append(
                RuleEvaluation(
                    "por.reserve_change",
                    change_level,
                    evidence,
                )
            )
        return evaluations

    async def _emit_and_evaluate_coverage(self, now: datetime) -> None:
        connection = self._storage.connection
        async with self._storage.write_lock:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                observation, evaluations = await self._coverage_update(now)
                if evaluations:
                    await StateEngine(self._storage).apply_uncommitted(
                        evaluations, now
                    )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    async def _persist_supplies(
        self,
        batch: SupplyBatch,
        now: datetime,
    ) -> None:
        connection = self._storage.connection
        async with self._storage.write_lock:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                for supply in batch.snapshots:
                    await self._storage.insert_observation_uncommitted(
                        supply.observation
                    )
                evaluations: list[RuleEvaluation] = []
                if batch.multichain_complete:
                    _, coverage_evaluations = await self._coverage_update(now)
                    evaluations.extend(coverage_evaluations)
                    evaluations.extend(
                        await self._native_supply_evaluations(now)
                    )
                    evaluations.extend(
                        await self._bridge_supply_evaluations(now)
                    )
                if evaluations:
                    await StateEngine(self._storage).apply_uncommitted(
                        evaluations, now
                    )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    async def _coverage_update(
        self, now: datetime
    ) -> tuple[Observation | None, list[RuleEvaluation]]:
        reserves = await self._storage.latest_observation("por.reserves", "ethereum")
        global_supply = await self._storage.latest_observation(
            "supply.multichain_total", "global"
        )
        if (
            reserves is None
            or global_supply is None
            or reserves.quality != "FACT"
            or global_supply.quality != "FACT"
        ):
            return None, []
        reserve_age = (
            _validated_observation_age(reserves.observed_at, now)
        )
        supply_age = (
            _validated_observation_age(global_supply.observed_at, now)
        )
        if (
            reserve_age >= self._por_config.coverage_max_age_seconds
            or supply_age > 4500
        ):
            return None, []
        coverage = estimated_coverage(
            reserves=reserves.value, global_supply=global_supply.value
        )
        observation = Observation(
            "supply.estimated_collateralization",
            "por+onchain_multichain",
            "global",
            coverage.ratio_percent,
            "percent",
            min(reserves.observed_at, global_supply.observed_at),
            now,
            quality=coverage.quality,
            metadata={
                "por_observed_at": reserves.observed_at.isoformat(),
                "supply_observed_at": global_supply.observed_at.isoformat(),
            },
        )
        await self._storage.insert_observation_uncommitted(observation)
        ratio_rows = await self._storage.latest_observations(
            "supply.estimated_collateralization", limit=2
        )
        ordered_ratios = list(reversed(ratio_rows))
        if len(ordered_ratios) == 2:
            gap = ordered_ratios[-1].observed_at - ordered_ratios[-2].observed_at
            max_continuous_gap = timedelta(
                seconds=self._supply_config.interval_seconds * 1.5
            )
            if gap > max_continuous_gap:
                ordered_ratios = ordered_ratios[-1:]
        market_state = await self._storage.get_risk_state("market.price")
        market_observation = await self._storage.latest_observation(
            "market.mid_price", "USD1USDT"
        )
        market_level: RiskLevel | CoverageState = CoverageState.UNKNOWN
        if (
            market_state is not None
            and market_observation is not None
            and (now - market_observation.observed_at).total_seconds() <= 300
        ):
            market_level = market_state.level
        result = evaluate_supply(
            SupplyRiskInput(
                [item.value for item in ordered_ratios],
                None,
                market_level,
                initial_coverage_level=(
                    coverage_state.level
                    if (
                        coverage_state := await self._storage.get_risk_state(
                            "supply.estimated_coverage"
                        )
                    )
                    is not None
                    else RiskLevel.GREEN
                ),
            )
        )
        evaluations: list[RuleEvaluation] = []
        if isinstance(result.coverage_level, RiskLevel):
            evidence = {
                "current": observation.value,
                "threshold": 100,
                "quality": "ESTIMATED",
                "data_time": observation.observed_at.isoformat(),
            }
            source_urls = _observation_source_urls(reserves, global_supply)
            if source_urls:
                evidence["source_urls"] = source_urls
            evaluations.append(
                RuleEvaluation(
                    "supply.estimated_coverage",
                    result.coverage_level,
                    evidence,
                )
            )
        return observation, evaluations

    async def _bridge_supply_evaluations(
        self,
        now: datetime,
    ) -> list[RuleEvaluation]:
        issued_rows = await self._storage.latest_observations(
            "supply.bridged_total",
            limit=4,
        )
        locked_rows = await self._storage.latest_observations(
            "bridge.locked_total",
            limit=4,
        )
        issued_by_time = {item.collected_at: item for item in issued_rows}
        locked_by_time = {item.collected_at: item for item in locked_rows}
        paired_times = sorted(set(issued_by_time) & set(locked_by_time))[-2:]
        if not paired_times:
            return []
        if len(paired_times) == 2:
            gap = paired_times[-1] - paired_times[-2]
            if gap > timedelta(
                seconds=self._supply_config.interval_seconds * 1.5
            ):
                paired_times = paired_times[-1:]
        readings = [
            BridgeReading(
                issued=issued_by_time[item].value,
                locked=locked_by_time[item].value,
            )
            for item in paired_times
        ]
        prior = await self._storage.get_risk_state(
            "supply.bridge_reconciliation"
        )
        result = evaluate_bridge_reconciliation(
            readings,
            prior.level if prior is not None else RiskLevel.GREEN,
        )
        current_time = paired_times[-1]
        issued = issued_by_time[current_time]
        locked = locked_by_time[current_time]
        return [
            RuleEvaluation(
                "supply.bridge_reconciliation",
                result.level,
                {
                    "direction": result.direction.value,
                    "issued": issued.value,
                    "locked": locked.value,
                    "difference": result.delta,
                    "difference_percent": result.ratio_percent,
                    "data_time": current_time.isoformat(),
                },
            )
        ]

    async def _native_supply_evaluations(
        self, now: datetime
    ) -> list[RuleEvaluation]:
        native_drop = await self._native_drop_24h(now)
        market_state = await self._storage.get_risk_state("market.price")
        market_observation = await self._storage.latest_observation(
            "market.mid_price", "USD1USDT"
        )
        market_level: RiskLevel | CoverageState = CoverageState.UNKNOWN
        if (
            market_state is not None
            and market_observation is not None
            and (now - market_observation.observed_at).total_seconds() <= 300
        ):
            market_level = market_state.level
        result = evaluate_supply(
            SupplyRiskInput([], native_drop, market_level)
        )
        if not isinstance(result.native_supply_level, RiskLevel):
            return []
        native_rows = await self._storage.latest_observations(
            "supply.native", limit=1000
        )
        latest_by_scope: dict[str, Observation] = {}
        for item in native_rows:
            latest_by_scope.setdefault(item.scope, item)
        evidence = {
            "current": native_drop,
            "threshold": 0.02,
            "data_time": now.isoformat(),
        }
        source_urls = _observation_source_urls(*latest_by_scope.values())
        if source_urls:
            evidence["source_urls"] = source_urls
        return [
            RuleEvaluation(
                "supply.native_drop_24h",
                result.native_supply_level,
                evidence,
            )
        ]

    async def _native_drop_24h(self, now: datetime) -> float | None:
        rows = await self._storage.latest_observations(
            "supply.multichain_total",
            limit=1000,
        )
        cutoff = now.timestamp() - 86400
        if not rows:
            return None
        current = rows[0]
        baseline = next(
            (
                item
                for item in rows
                if item.observed_at.timestamp() <= cutoff
            ),
            None,
        )
        if baseline is None:
            return None
        freshness_seconds = self._supply_config.interval_seconds + 900
        cutoff_time = now - timedelta(days=1)
        if (
            (now - current.observed_at).total_seconds() > freshness_seconds
            or (cutoff_time - baseline.observed_at).total_seconds()
            > freshness_seconds
        ):
            return None
        before = baseline.value
        after = current.value
        if before <= 0:
            return None
        return max(0.0, (before - after) / before)


class OfficialSource(Protocol):
    async def collect(self, collected_at: datetime) -> list[Announcement]: ...


class InformationMonitor:
    def __init__(
        self,
        sources: dict[str, OfficialSource],
        storage: Storage,
        notifier: Notifier | None,
        *,
        intervals: dict[str, int],
    ) -> None:
        self._sources = sources
        self._storage = storage
        self._notifier = notifier
        self._intervals = intervals
        self._last_runs: dict[str, datetime] = {}

    @property
    def tick_interval_seconds(self) -> int:
        return min(self._intervals.values())

    async def check_once(
        self,
        *,
        deliver: bool = True,
        now: datetime | None = None,
    ) -> CheckResult:
        checked_at = now or datetime.now(UTC)
        checks: list[Awaitable[CheckResult]] = []
        for source_name, source in self._sources.items():
            previous = self._last_runs.get(source_name)
            interval = self._intervals[source_name]
            if previous is not None and (checked_at - previous).total_seconds() < interval:
                continue
            checks.append(
                self._check_source(source_name, source, checked_at)
            )
        results = list(await asyncio.gather(*checks)) if checks else []
        if deliver and self._notifier is not None:
            await _deliver_pending(self._storage, self._notifier)
        errors = tuple(error for result in results for error in result.errors)
        details = tuple(detail for result in results for detail in result.details)
        return CheckResult(not errors, errors, details)

    async def _check_source(
        self,
        source_name: str,
        source: OfficialSource,
        checked_at: datetime,
    ) -> CheckResult:
        health_key = f"official_{source_name}"
        try:
            items = await source.collect(checked_at)
            await self._persist_items(source_name, items, checked_at)
            await _record_health(
                self._storage, health_key, checked_at, success=True
            )
            self._last_runs[source_name] = checked_at
            return CheckResult(
                True, (), (f"official_{source_name} items={len(items)}",)
            )
        except BinancePartialCollectionError as exc:
            for stable_id, failure in exc.failures:
                logger.error(
                    "official source item failed source=%s stable_id=%s",
                    source_name,
                    stable_id,
                    exc_info=(
                        type(failure),
                        failure,
                        failure.__traceback__,
                    ),
                )
            try:
                await self._persist_items(source_name, exc.items, checked_at)
            except Exception as persist_exc:
                logger.exception(
                    "official source partial results persistence failed source=%s",
                    source_name,
                )
                error = (
                    f"official_{source_name}: {type(persist_exc).__name__}: "
                    f"{persist_exc}"
                )
                await _record_health(
                    self._storage,
                    health_key,
                    checked_at,
                    success=False,
                    error=error,
                )
                return CheckResult(False, (error,))
            error = f"official_{source_name}: {type(exc).__name__}: {exc}"
            await _record_health(
                self._storage,
                health_key,
                checked_at,
                success=False,
                error=error,
            )
            self._last_runs[source_name] = checked_at
            return CheckResult(
                False,
                (error,),
                (f"official_{source_name} partial_items={len(exc.items)}",),
            )
        except Exception as exc:
            logger.exception(
                "official source collection failed source=%s", source_name
            )
            error = f"official_{source_name}: {type(exc).__name__}: {exc}"
            await _record_health(
                self._storage,
                health_key,
                checked_at,
                success=False,
                error=error,
            )
            return CheckResult(False, (error,))

    async def _persist_items(
        self,
        source_name: str,
        items: list[Announcement],
        checked_at: datetime,
    ) -> None:
        evaluations: list[RuleEvaluation] = []
        source_health = await self._storage.get_collector_health(
            f"official_{source_name}"
        )
        source_has_baseline = (
            source_health is not None
            and source_health.last_success_at is not None
        )
        previously_scanned_binance_ids = (
            await self._storage.announcement_stable_ids("binance_scan")
            if source_name == "binance"
            else set()
        )
        failed_binance_scan_ids = (
            await self._storage.recent_announcement_failure_ids(
                "binance_scan", datetime.min.replace(tzinfo=UTC)
            )
            if source_name == "binance"
            else set()
        )
        successful_binance_scan_ids = (
            previously_scanned_binance_ids - failed_binance_scan_ids
        )
        previous_attestation = (
            await self._storage.latest_announcement("bitgo")
            if source_name == "bitgo"
            else None
        )
        changed_bitgo_ids: set[str] = set()
        connection = self._storage.connection
        async with self._storage.write_lock:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                for item in items:
                    outcome = await self._storage.upsert_announcement_uncommitted(item)
                    if outcome in {"UNCHANGED", "BASELINED"}:
                        continue
                    if source_name == "bitgo":
                        changed_bitgo_ids.add(item.stable_id)
                    promoted_binance_scan = (
                        source_name == "binance"
                        and item.stable_id in successful_binance_scan_ids
                    )
                    if outcome == "NEW" and not promoted_binance_scan:
                        if item.published_at is not None:
                            if item.published_at < (
                                checked_at - _NEW_INFORMATION_ALERT_MAX_AGE
                            ):
                                continue
                        elif not source_has_baseline:
                            continue
                    body_text = str(item.metadata.get("body_text", ""))
                    raw_sections = item.metadata.get("body_sections")
                    body_sections = (
                        [
                            section
                            for section in raw_sections
                            if isinstance(section, str) and section.strip()
                        ]
                        if isinstance(raw_sections, list)
                        else [body_text]
                    )
                    if not body_sections:
                        body_sections = [body_text]
                    classification = classify_official_text(item.title)
                    for section in body_sections:
                        if classification.level is RiskLevel.YELLOW:
                            break
                        classification = classify_official_text(section)
                    if classification.level is RiskLevel.YELLOW:
                        evaluations.append(
                            RuleEvaluation(
                                f"event.information.{source_name}.{item.stable_id}.{item.body_hash[:12]}",
                                RiskLevel.YELLOW,
                                {
                                    "current": outcome,
                                    "threshold": (
                                        "official risk phrase: "
                                        f"{classification.evidence}"
                                    ),
                                    "data_time": checked_at.isoformat(),
                                    "source_url": item.url,
                                    "title": item.title,
                                },
                                f"official:{source_name}:{item.stable_id}:{item.body_hash}",
                            )
                        )
                if source_name == "bitgo":
                    latest = await self._storage.latest_announcement("bitgo")
                    if latest is None:
                        raise ValueError("BitGo yielded no persisted attestation")
                    parse_error = latest.metadata.get("parse_error")
                    evaluations.append(
                        RuleEvaluation(
                            "information.attestation_parse",
                            (
                                RiskLevel.YELLOW
                                if parse_error
                                else RiskLevel.GREEN
                            ),
                            {
                                "current": str(parse_error or "parsed"),
                                "threshold": "all required attestation fields parsed",
                                "data_time": checked_at.isoformat(),
                                "source_url": latest.url,
                            },
                            f"official:bitgo:{latest.stable_id}:{latest.body_hash}",
                        )
                    )
                    if (
                        previous_attestation is not None
                        and latest.stable_id in changed_bitgo_ids
                        and not parse_error
                        and not previous_attestation.metadata.get("parse_error")
                    ):
                        changed_fields = [
                            field
                            for field in (
                                "asset_categories",
                                "auditor",
                                "custodian",
                            )
                            if previous_attestation.metadata.get(field)
                            != latest.metadata.get(field)
                        ]
                        if changed_fields:
                            evaluations.append(
                                RuleEvaluation(
                                    f"event.information.bitgo.attestation_fields.{latest.stable_id}",
                                    RiskLevel.YELLOW,
                                    {
                                        "current": changed_fields,
                                        "threshold": "attestation identity fields unchanged",
                                        "data_time": checked_at.isoformat(),
                                        "source_url": latest.url,
                                    },
                                    f"official:bitgo:{latest.stable_id}:{latest.body_hash}",
                                )
                            )
                    report_month = latest.stable_id.split(":", 1)[0]
                    due_at = next_attestation_due_at(report_month)
                    evaluations.append(
                        RuleEvaluation(
                            "information.attestation_late",
                            (
                                RiskLevel.YELLOW
                                if checked_at.date() > due_at
                                else RiskLevel.GREEN
                            ),
                            {
                                "current": report_month,
                                "threshold": due_at.isoformat(),
                                "data_time": checked_at.isoformat(),
                                "source_url": latest.url,
                            },
                            "attestation_lateness",
                        )
                    )
                if evaluations:
                    await StateEngine(self._storage).apply_uncommitted(
                        evaluations, checked_at
                    )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise


async def _record_health(
    storage: Storage,
    collector_id: str,
    now: datetime,
    *,
    success: bool,
    error: str | None = None,
    critical: bool = False,
    enqueue_alerts: bool = True,
    extra_evidence: dict[str, object] | None = None,
) -> None:
    risk_state = await storage.get_risk_state(f"health.{collector_id}")
    previous_level = risk_state.level if risk_state is not None else RiskLevel.GREEN
    if success:
        await storage.record_collector_success(collector_id, now)
    else:
        await storage.record_collector_failure(
            collector_id, now, error or "unknown collector failure"
        )
    health = await storage.get_collector_health(collector_id)
    assert health is not None
    level = evaluate_health_with_recovery(
        health,
        now,
        critical=critical,
        previous_level=previous_level,
    )
    if collector_id == "notification_wechat" and await storage.failed_alerts():
        level = max(level, RiskLevel.YELLOW)
    evidence: dict[str, object] = {
        "current": health.consecutive_failures,
        "threshold": 3,
        "data_time": now.isoformat(),
        "last_error": health.last_error,
    }
    if extra_evidence:
        evidence.update(extra_evidence)
    await StateEngine(storage).apply(
        [
            RuleEvaluation(
                f"health.{collector_id}",
                level,
                evidence,
            )
        ],
        now,
        enqueue_alerts=enqueue_alerts,
    )
