from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum, StrEnum
from typing import Any


class RiskLevel(IntEnum):
    GREEN = 0
    YELLOW = 1
    RED = 2


class CoverageState(StrEnum):
    MONITORED = "MONITORED"
    UNKNOWN = "UNKNOWN"
    NOT_MONITORED = "NOT_MONITORED"


@dataclass(frozen=True)
class Observation:
    metric: str
    source: str
    scope: str
    value: float
    unit: str
    observed_at: datetime
    collected_at: datetime
    quality: str = "FACT"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RiskState:
    rule_id: str
    level: RiskLevel
    first_triggered_at: datetime
    changed_at: datetime


@dataclass(frozen=True)
class RuleEvaluation:
    rule_id: str
    level: RiskLevel
    evidence: dict[str, Any] = field(default_factory=dict)
    cause_id: str | None = None


@dataclass(frozen=True)
class RiskTransition:
    rule_id: str
    previous: RiskLevel
    current: RiskLevel
    changed_at: datetime
    first_triggered_at: datetime
    evidence: dict[str, Any] = field(default_factory=dict)
    cause_id: str | None = None


@dataclass(frozen=True)
class PendingAlert:
    id: int
    alert_key: str
    content: str
    attempts: int


@dataclass(frozen=True)
class ChainEvent:
    chain: str
    block_number: int
    tx_hash: str
    log_index: int
    event_type: str
    payload: dict[str, Any]
    observed_at: datetime


@dataclass(frozen=True)
class Announcement:
    source: str
    stable_id: str
    title: str
    url: str
    published_at: datetime | None
    body_hash: str
    first_seen_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)
