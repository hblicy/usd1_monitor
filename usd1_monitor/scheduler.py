from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from usd1_monitor import __version__
from usd1_monitor.config import MarketConfig
from usd1_monitor.collectors.evm import (
    EvmScanner,
    EvmSnapshot,
    EvmSnapshotReader,
    PrivilegedCallCollector,
)
from usd1_monitor.collectors.announcements import (
    BinancePartialCollectionError,
    OfficialPageCollector,
)
from usd1_monitor.collectors.reserves import PorCollector, PorSnapshot
from usd1_monitor.collectors.supply import (
    DefiLlamaSupplyCollector,
    NativeSupplyCollector,
    SupplyDataError,
    SupplySnapshot,
    estimated_coverage,
)
from usd1_monitor.config import PorConfig, SupplyConfig
from usd1_monitor.engine.evm_rules import (
    EXPLORER_BASE_URLS,
    EvmFact,
    evaluate_evm_fact,
)
from usd1_monitor.engine.health import evaluate_health
from usd1_monitor.engine.information_rules import (
    classify_official_text,
    next_attestation_due_at,
)
from usd1_monitor.engine.reserve_rules import (
    PorReading,
    evaluate_por,
    evaluate_reserve_change,
)
from usd1_monitor.engine.supply_rules import SupplyRiskInput, evaluate_supply
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
from usd1_monitor.storage import Storage


logger = logging.getLogger(__name__)
_NOT_COLLECTED = object()
_NEW_INFORMATION_ALERT_MAX_AGE = timedelta(hours=24)

NOT_MONITORED = (
    "private_exchange_account",
    "active_conversion_probe",
    "tron_solana_aptos_tempo_bridges",
    "binance_wallet_concentration",
    "social_media_sentiment",
    "defi_liquidations",
    "web_dashboard",
    "full_multichain_supply_reconciliation",
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


@dataclass(frozen=True)
class CheckResult:
    success: bool
    errors: tuple[str, ...]
    details: tuple[str, ...] = ()


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
        content = (
            f"USD1 monitor {__version__} 已启动\n"
            "enabled_collectors: binance_market\n"
            f"NOT_MONITORED: {', '.join(NOT_MONITORED)}"
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
            snapshot = await self._snapshot_reader.read(processed_head, checked_at)
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
            result = await asyncio.wait_for(
                check(), timeout=self._check_timeout_seconds
            )
        except TimeoutError:
            error = (
                f"{name}: TimeoutError: exceeded "
                f"{self._check_timeout_seconds:g}s"
            )
            logger.error("monitor component timed out component=%s", name)
            await _record_health(
                self._storage,
                f"scheduler_{name}",
                datetime.now(UTC),
                success=False,
                error=error,
                critical=True,
            )
            return CheckResult(False, (error,))
        await _record_health(
            self._storage,
            f"scheduler_{name}",
            datetime.now(UTC),
            success=True,
            critical=True,
        )
        return result

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
        enabled = "binance_market, " + ", ".join(
            f"evm_{item.chain}" for item in self._evm_chains
        )
        if self._reserve_supply is not None:
            enabled += ", por, native_supply, defillama_supply"
        if self._information is not None:
            enabled += ", official_binance, official_bitgo, official_wlfi, official_occ"
        try:
            await self._notifier.send_text(
                f"USD1 monitor {__version__} 已启动\n"
                f"enabled_collectors: {enabled}\n"
                f"NOT_MONITORED: {', '.join(NOT_MONITORED)}"
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


class CombinedSupplySource:
    def __init__(
        self,
        native_sources: list[tuple[object, NativeSupplyCollector, int]],
        global_source: DefiLlamaSupplyCollector,
    ) -> None:
        self._native_sources = native_sources
        self._global_source = global_source

    async def collect(self, collected_at: datetime) -> SupplyBatch:
        jobs: list[tuple[str, Awaitable[SupplySnapshot]]] = [
            (
                collector.chain,
                self._collect_native(
                    rpc, collector, confirmation_depth, collected_at
                ),
            )
            for rpc, collector, confirmation_depth in self._native_sources
        ]
        jobs.append(("defillama", self._global_source.collect(collected_at)))
        results = await asyncio.gather(
            *(job for _, job in jobs), return_exceptions=True
        )
        snapshots: list[SupplySnapshot] = []
        errors: list[tuple[str, Exception]] = []
        for (source, _), result in zip(jobs, results, strict=True):
            if isinstance(result, Exception):
                errors.append((source, result))
            elif isinstance(result, BaseException):
                raise result
            else:
                snapshots.append(result)
        return SupplyBatch(tuple(snapshots), tuple(errors))

    @staticmethod
    async def _collect_native(
        rpc: object,
        collector: NativeSupplyCollector,
        confirmation_depth: int,
        collected_at: datetime,
    ) -> SupplySnapshot:
        latest = int(await rpc.call("eth_blockNumber", []), 16)  # type: ignore[attr-defined]
        safe_block = latest - confirmation_depth
        if safe_block < 0:
            raise ValueError(f"{collector.chain} has no confirmed supply block")
        return await collector.collect(safe_block, collected_at)


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
                "supply.global", "global"
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
                update_coverage=False,
            )
            supply_result = await self._check_supply(
                checked_at,
                collected=collected[1],
                update_coverage=por_result.success,
                force_coverage=por_result.success,
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
        update_coverage: bool = True,
    ) -> CheckResult:
        try:
            if collected is _NOT_COLLECTED:
                por = await self._por.collect(checked_at)
            elif isinstance(collected, BaseException):
                raise collected
            else:
                por = collected
            await self._persist_por(
                por,
                checked_at,
                update_coverage=update_coverage,
            )
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
        update_coverage: bool = True,
        force_coverage: bool = False,
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
                            supply.scope,
                            SupplyDataError(
                                f"safe block is older than {native_max_age}s"
                            ),
                        )
                    )
                    continue
                valid_supplies.append(supply)
            batch = SupplyBatch(tuple(valid_supplies), tuple(batch_errors))
            supplies = batch.snapshots
            await self._persist_supplies(
                supplies,
                checked_at,
                update_coverage=update_coverage,
                force_coverage=force_coverage,
            )
            errors: list[str] = []
            details: list[str] = []
            for supply in supplies:
                source_name = (
                    supply.scope
                    if supply.scope in {"ethereum", "bsc"}
                    else supply.observation.source
                )
                await _record_health(
                    self._storage,
                    f"supply_{source_name}",
                    checked_at,
                    success=True,
                )
                details.append(
                    f"supply {supply.scope}={supply.supply} "
                    f"quality={supply.observation.quality}"
                )
            for source, exc in batch.errors:
                error = f"supply_{source}: {type(exc).__name__}: {exc}"
                errors.append(error)
                await _record_health(
                    self._storage,
                    f"supply_{source}",
                    checked_at,
                    success=False,
                    error=error,
                )
            await _record_health(
                self._storage,
                "supply",
                checked_at,
                success=not batch.errors,
                error="; ".join(errors) if batch.errors else None,
            )
            self._last_supply_run = checked_at
            return CheckResult(not errors, tuple(errors), tuple(details))
        except Exception as exc:
            logger.exception("supply collection failed")
            error = f"supply: {type(exc).__name__}: {exc}"
            await _record_health(
                self._storage,
                "supply",
                checked_at,
                success=False,
                error=error,
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
        *,
        update_coverage: bool = True,
    ) -> None:
        connection = self._storage.connection
        async with self._storage.write_lock:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                for observation in por.observations:
                    await self._storage.insert_observation_uncommitted(observation)
                evaluations = await self._por_evaluations(now)
                if update_coverage:
                    _, coverage_evaluations = await self._coverage_update(now)
                    evaluations.extend(coverage_evaluations)
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
            prior_age = await self._storage.get_risk_state("por.age")
            age_level = result.age_level
            if (
                age_level is RiskLevel.GREEN
                and prior_age is not None
                and prior_age.level is not RiskLevel.GREEN
                and not result.valid_recovery
            ):
                age_level = prior_age.level
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
                    age_level,
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
        supplies: tuple[SupplySnapshot, ...],
        now: datetime,
        *,
        update_coverage: bool = True,
        force_coverage: bool = False,
    ) -> None:
        connection = self._storage.connection
        async with self._storage.write_lock:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                for supply in supplies:
                    await self._storage.insert_observation_uncommitted(
                        supply.observation
                    )
                has_current_global = any(
                    supply.observation.metric == "supply.global"
                    and supply.scope == "global"
                    for supply in supplies
                )
                evaluations: list[RuleEvaluation] = []
                if update_coverage and has_current_global:
                    _, coverage_evaluations = await self._coverage_update(
                        now, force=force_coverage
                    )
                    evaluations.extend(coverage_evaluations)
                has_current_native = any(
                    supply.observation.metric == "supply.native"
                    and supply.scope in {"ethereum", "bsc"}
                    for supply in supplies
                )
                if has_current_native:
                    evaluations.extend(
                        await self._native_supply_evaluations(now)
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
        self, now: datetime, *, force: bool = False
    ) -> tuple[Observation | None, list[RuleEvaluation]]:
        latest_ratio = await self._storage.latest_observation(
            "supply.estimated_collateralization", "global"
        )
        replace_latest_ratio = (
            force
            and latest_ratio is not None
            and (now - latest_ratio.collected_at).total_seconds()
            < self._supply_config.interval_seconds
        )
        if (
            not force
            and latest_ratio is not None
            and (now - latest_ratio.collected_at).total_seconds()
            < self._supply_config.interval_seconds
        ):
            return None, []
        reserves = await self._storage.latest_observation("por.reserves", "ethereum")
        global_supply = await self._storage.latest_observation(
            "supply.global", "global"
        )
        if (
            reserves is None
            or global_supply is None
            or (now - reserves.observed_at).total_seconds() > 4500
            or (now - global_supply.observed_at).total_seconds() > 4500
        ):
            return None, []
        coverage = estimated_coverage(
            reserves=reserves.value, global_supply=global_supply.value
        )
        observation = Observation(
            "supply.estimated_collateralization",
            "por+defillama",
            "global",
            coverage.ratio_percent,
            "percent",
            now,
            now,
            quality=coverage.quality,
        )
        if replace_latest_ratio:
            await self._storage.delete_latest_observation_uncommitted(
                "supply.estimated_collateralization", "global"
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
                "data_time": now.isoformat(),
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
        rows = await self._storage.latest_observations("supply.native", limit=1000)
        cutoff = now.timestamp() - 86400
        current: dict[str, Observation] = {}
        baseline: dict[str, Observation] = {}
        for item in rows:
            current.setdefault(item.scope, item)
            if item.observed_at.timestamp() <= cutoff:
                baseline.setdefault(item.scope, item)
        if set(current) != {"ethereum", "bsc"} or set(baseline) != {
            "ethereum",
            "bsc",
        }:
            return None
        freshness_seconds = self._supply_config.interval_seconds + 900
        cutoff_time = now - timedelta(days=1)
        if any(
            (now - item.observed_at).total_seconds() > freshness_seconds
            for item in current.values()
        ) or any(
            (cutoff_time - item.observed_at).total_seconds() > freshness_seconds
            for item in baseline.values()
        ):
            return None
        before = sum(item.value for item in baseline.values())
        after = sum(item.value for item in current.values())
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
) -> None:
    if success:
        await storage.record_collector_success(collector_id, now)
    else:
        await storage.record_collector_failure(
            collector_id, now, error or "unknown collector failure"
        )
    health = await storage.get_collector_health(collector_id)
    assert health is not None
    level = evaluate_health(health, now, critical=critical)
    if collector_id == "notification_wechat" and await storage.failed_alerts():
        level = max(level, RiskLevel.YELLOW)
    await StateEngine(storage).apply(
        [
            RuleEvaluation(
                f"health.{collector_id}",
                level,
                {
                    "current": health.consecutive_failures,
                    "threshold": 3,
                    "data_time": now.isoformat(),
                    "last_error": health.last_error,
                },
            )
        ],
        now,
    )
