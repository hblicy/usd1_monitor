from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from usd1_monitor.models import CoverageState, Observation, RiskLevel


CRITICAL_PILLAR_NAMES = frozenset({"concentration", "coverage", "redemption"})
POR_COVERAGE_MAX_AGE_SECONDS = 1800
SUPPLY_MAX_AGE_SECONDS = 4500
CONCENTRATION_DEFAULT_MAX_AGE_SECONDS = 1200
REDEMPTION_DEFAULT_MAX_AGE_SECONDS = 900


@dataclass(frozen=True)
class Pillar:
    name: str
    available: bool
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("pillar name must not be empty")
        if self.name not in CRITICAL_PILLAR_NAMES:
            raise ValueError(f"unknown pillar name: {self.name}")
        if type(self.available) is not bool:
            raise TypeError("pillar available must be a bool")


@dataclass(frozen=True)
class AssetAssessment:
    level: RiskLevel | CoverageState
    missing_pillars: tuple[Pillar, ...]


def _observation_age_seconds(observation: Observation, now: datetime) -> float:
    if (
        observation.observed_at.tzinfo is None
        or observation.observed_at.utcoffset() is None
    ):
        raise ValueError("observation timestamp must be timezone-aware")
    age = (
        now.astimezone(UTC) - observation.observed_at.astimezone(UTC)
    ).total_seconds()
    if age < 0:
        raise ValueError("observation timestamp must not be in the future")
    return age


def _is_fresh_with_metadata(
    observation: Observation | None,
    now: datetime,
) -> bool:
    if observation is None:
        return False
    age = _observation_age_seconds(observation, now)
    if not isinstance(observation.metadata, dict):
        return False
    max_age = observation.metadata.get("max_age_seconds")
    if type(max_age) is not int or max_age <= 0:
        return False
    return age < max_age


def pillar_from_observations(
    *,
    now: datetime,
    coverage_max_age_seconds: int,
    concentration: Observation | None,
    reserves: Observation | None,
    supply: Observation | None,
    redemption: Observation | None,
) -> list[Pillar]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("current timestamp must be timezone-aware")
    if (
        type(coverage_max_age_seconds) is not int
        or coverage_max_age_seconds <= 0
    ):
        raise ValueError("coverage_max_age_seconds must be a positive integer")

    concentration_ready = _is_fresh_with_metadata(concentration, now)
    redemption_ready = _is_fresh_with_metadata(redemption, now)
    reserves_age = (
        _observation_age_seconds(reserves, now) if reserves is not None else None
    )
    supply_age = (
        _observation_age_seconds(supply, now) if supply is not None else None
    )
    coverage_ready = (
        reserves is not None
        and supply is not None
        and reserves.quality == "FACT"
        and supply.quality == "FACT"
        and reserves_age is not None
        and reserves_age < coverage_max_age_seconds
        and supply_age is not None
        and supply_age <= SUPPLY_MAX_AGE_SECONDS
    )
    return [
        Pillar(
            "concentration",
            concentration_ready,
            None if concentration_ready else "custody_unavailable",
        ),
        Pillar(
            "coverage",
            coverage_ready,
            None if coverage_ready else "coverage_unavailable",
        ),
        Pillar(
            "redemption",
            redemption_ready,
            None if redemption_ready else "redemption_unavailable",
        ),
    ]


def assess_asset(
    known_level: RiskLevel,
    pillars: list[Pillar],
) -> AssetAssessment:
    if type(known_level) is not RiskLevel:
        raise TypeError("known_level must be a RiskLevel")
    if any(not isinstance(pillar, Pillar) for pillar in pillars):
        raise TypeError("pillars must contain Pillar values")
    names = [pillar.name for pillar in pillars]
    if (
        len(names) != len(CRITICAL_PILLAR_NAMES)
        or set(names) != CRITICAL_PILLAR_NAMES
    ):
        raise ValueError(
            "pillar names must be unique and each critical pillar must appear "
            "exactly once"
        )

    missing = tuple(pillar for pillar in pillars if not pillar.available)
    if known_level is RiskLevel.RED:
        return AssetAssessment(RiskLevel.RED, missing)
    if known_level is RiskLevel.YELLOW:
        return AssetAssessment(RiskLevel.YELLOW, missing)
    if missing:
        return AssetAssessment(CoverageState.UNKNOWN, missing)
    return AssetAssessment(RiskLevel.GREEN, ())
