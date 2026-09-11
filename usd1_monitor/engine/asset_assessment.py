from __future__ import annotations

from dataclasses import dataclass

from usd1_monitor.models import CoverageState, RiskLevel


CRITICAL_PILLAR_NAMES = frozenset({"concentration", "coverage", "redemption"})


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
