from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from usd1_monitor.models import RiskLevel


@dataclass(frozen=True)
class Transfer:
    sender: str
    receiver: str
    amount: float
    observed_at: datetime


@dataclass(frozen=True)
class CustodyFlow:
    entity_net_24h: float
    address_outflow_1h: dict[str, float]


@dataclass(frozen=True)
class RuleDecision:
    level: RiskLevel
    clear_checks: int


def summarize_transfers(
    transfers: list[Transfer],
    group: frozenset[str],
    now: datetime,
) -> CustodyFlow:
    normalized = frozenset(address.casefold() for address in group)
    entity_net = 0.0
    address_outflow = {address: 0.0 for address in normalized}
    entity_window_start = now - timedelta(hours=24)
    address_window_start = now - timedelta(hours=1)

    for transfer in transfers:
        if transfer.amount < 0:
            raise ValueError("transfer amount must not be negative")
        if transfer.observed_at > now:
            raise ValueError("transfer observed_at must not be in the future")

        sender = transfer.sender.casefold()
        receiver = transfer.receiver.casefold()
        sender_inside = sender in normalized
        receiver_inside = receiver in normalized

        if (
            transfer.observed_at >= entity_window_start
            and sender_inside != receiver_inside
        ):
            entity_net += -transfer.amount if sender_inside else transfer.amount
        if (
            transfer.observed_at >= address_window_start
            and sender_inside
            and not receiver_inside
        ):
            address_outflow[sender] += transfer.amount

    return CustodyFlow(entity_net, address_outflow)


def _with_recovery(
    current: RiskLevel,
    previous: RiskLevel,
    clear_checks: int,
    *,
    recovery_checks: int = 2,
) -> RuleDecision:
    if current is not RiskLevel.GREEN:
        return RuleDecision(current, 0)
    if previous is RiskLevel.GREEN:
        return RuleDecision(RiskLevel.GREEN, 0)

    next_clear = clear_checks + 1
    if next_clear < recovery_checks:
        return RuleDecision(previous, next_clear)
    return RuleDecision(RiskLevel.GREEN, 0)


def evaluate_concentration(
    share: float,
    previous: RiskLevel,
    clear_checks: int,
    *,
    yellow: float = 0.50,
    red: float = 0.70,
    recovery_checks: int = 2,
) -> RuleDecision:
    current = (
        RiskLevel.RED
        if share > red
        else RiskLevel.YELLOW
        if share > yellow
        else RiskLevel.GREEN
    )
    return _with_recovery(
        current,
        previous,
        clear_checks,
        recovery_checks=recovery_checks,
    )


def evaluate_entity_flow(
    value: float,
    previous: RiskLevel,
    clear_checks: int,
    *,
    threshold: float = 50_000_000,
    recovery_checks: int = 2,
) -> RuleDecision:
    current = (
        RiskLevel.YELLOW if abs(value) > threshold else RiskLevel.GREEN
    )
    return _with_recovery(
        current,
        previous,
        clear_checks,
        recovery_checks=recovery_checks,
    )


def evaluate_address_outflow(
    values: dict[str, float],
    previous: RiskLevel,
    clear_checks: int,
    *,
    threshold: float = 100_000_000,
    recovery_checks: int = 2,
) -> RuleDecision:
    current = (
        RiskLevel.YELLOW
        if max(values.values(), default=0) > threshold
        else RiskLevel.GREEN
    )
    return _with_recovery(
        current,
        previous,
        clear_checks,
        recovery_checks=recovery_checks,
    )
