import pytest

from usd1_monitor.engine.asset_assessment import Pillar, assess_asset
from usd1_monitor.models import CoverageState, RiskLevel


def test_asset_assessment_priority_is_red_yellow_unknown_green() -> None:
    ready = [
        Pillar("concentration", True),
        Pillar("coverage", True),
        Pillar("redemption", True),
    ]
    missing = [
        Pillar("concentration", True),
        Pillar("coverage", False, "por_stale"),
        Pillar("redemption", True),
    ]

    assert assess_asset(RiskLevel.RED, missing).level is RiskLevel.RED
    assert assess_asset(RiskLevel.YELLOW, missing).level is RiskLevel.YELLOW
    assert assess_asset(RiskLevel.GREEN, missing).level is CoverageState.UNKNOWN
    assert assess_asset(RiskLevel.GREEN, ready).level is RiskLevel.GREEN


def test_asset_assessment_preserves_all_missing_pillars_in_input_order() -> None:
    pillars = [
        Pillar("redemption", False, "source_stale"),
        Pillar("concentration", True),
        Pillar("coverage", False, "por_stale"),
    ]

    result = assess_asset(RiskLevel.GREEN, pillars)

    assert result.missing_pillars == (pillars[0], pillars[2])


def test_pillar_name_must_not_be_empty() -> None:
    with pytest.raises(ValueError, match="pillar name must not be empty"):
        Pillar("  ", False)


def test_asset_assessment_rejects_duplicate_pillar_names() -> None:
    with pytest.raises(ValueError, match="pillar names must be unique"):
        assess_asset(
            RiskLevel.GREEN,
            [Pillar("coverage", True), Pillar("coverage", False, "por_stale")],
        )


@pytest.mark.parametrize("known_level", [0, "GREEN", CoverageState.UNKNOWN, None])
def test_asset_assessment_rejects_non_risk_known_level(known_level) -> None:
    with pytest.raises(TypeError, match="known_level must be a RiskLevel"):
        assess_asset(
            known_level,
            [
                Pillar("concentration", True),
                Pillar("coverage", True),
                Pillar("redemption", True),
            ],
        )


@pytest.mark.parametrize(
    "pillars",
    [
        [Pillar("concentration", True), Pillar("coverage", True)],
        [
            Pillar("concentration", True),
            Pillar("coverage", True),
            Pillar("redemption", True),
            Pillar("redemption", True),
        ],
    ],
)
def test_asset_assessment_requires_each_critical_pillar_once(pillars) -> None:
    with pytest.raises(ValueError, match="exactly once"):
        assess_asset(RiskLevel.GREEN, pillars)


def test_pillar_rejects_unknown_name_and_non_boolean_availability() -> None:
    with pytest.raises(ValueError, match="unknown pillar name"):
        Pillar("liquidity", True)
    with pytest.raises(TypeError, match="available must be a bool"):
        Pillar("coverage", "false")
