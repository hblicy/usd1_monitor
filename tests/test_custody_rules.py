from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from usd1_monitor.engine.custody_rules import (
    CustodyFlow,
    RuleDecision,
    Transfer,
    evaluate_address_outflow,
    evaluate_concentration,
    evaluate_entity_flow,
    summarize_transfers,
)
from usd1_monitor.models import RiskLevel


NOW = datetime(2026, 9, 11, 4, tzinfo=UTC)
GROUP = frozenset({"0xAaA", "0xBbB"})


def test_rule_value_types_are_frozen() -> None:
    transfer = Transfer("0xaaa", "0xbbb", 1, NOW)
    flow = CustodyFlow(1, {"0xaaa": 2})
    decision = RuleDecision(RiskLevel.GREEN, 0)

    with pytest.raises(FrozenInstanceError):
        transfer.amount = 2  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        flow.entity_net_24h = 2  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        decision.level = RiskLevel.YELLOW  # type: ignore[misc]


def test_internal_transfers_are_excluded_from_all_flow_totals() -> None:
    transfers = [
        Transfer("0xAAA", "0xbbb", 70_000_000, NOW - timedelta(minutes=5)),
        Transfer("0xbbb", "0xAaA", 25_000_000, NOW - timedelta(minutes=10)),
        Transfer("0xccc", "0xaaa", 60_000_000, NOW - timedelta(hours=2)),
    ]

    result = summarize_transfers(transfers, GROUP, NOW)

    assert result.entity_net_24h == 60_000_000
    assert result.address_outflow_1h == {"0xaaa": 0, "0xbbb": 0}


def test_external_flows_use_expected_sign_and_per_address_outflow() -> None:
    transfers = [
        Transfer("0xexternal", "0xAAA", 80_000_000, NOW - timedelta(minutes=30)),
        Transfer("0xbbb", "0xoutside", 25_000_000, NOW - timedelta(minutes=20)),
        Transfer("0xaaa", "0xoutside", 10_000_000, NOW - timedelta(hours=2)),
    ]

    result = summarize_transfers(transfers, GROUP, NOW)

    assert result.entity_net_24h == 45_000_000
    assert result.address_outflow_1h == {
        "0xaaa": -80_000_000,
        "0xbbb": 25_000_000,
    }


def test_external_inflow_offsets_same_address_outflow_before_alerting() -> None:
    transfers = [
        Transfer("0xoutside", "0xaaa", 200_000_000, NOW - timedelta(minutes=30)),
        Transfer("0xaaa", "0xoutside", 100_000_001, NOW - timedelta(minutes=10)),
    ]

    flow = summarize_transfers(transfers, GROUP, NOW)

    assert flow.address_outflow_1h["0xaaa"] == -99_999_999
    assert evaluate_address_outflow(
        flow.address_outflow_1h,
        RiskLevel.GREEN,
        0,
    ) == RuleDecision(RiskLevel.GREEN, 0)


def test_flow_windows_include_exact_boundaries_and_exclude_older_records() -> None:
    transfers = [
        Transfer("0xoutside", "0xaaa", 40, NOW - timedelta(hours=24)),
        Transfer(
            "0xoutside",
            "0xaaa",
            1_000,
            NOW - timedelta(hours=24, microseconds=1),
        ),
        Transfer("0xbbb", "0xoutside", 10, NOW - timedelta(hours=1)),
        Transfer(
            "0xbbb",
            "0xoutside",
            1_000,
            NOW - timedelta(hours=1, microseconds=1),
        ),
    ]

    result = summarize_transfers(transfers, GROUP, NOW)

    assert result.entity_net_24h == -970
    assert result.address_outflow_1h == {"0xaaa": 0, "0xbbb": 10}


def test_empty_inputs_produce_zero_flow_and_normalized_group_keys() -> None:
    assert summarize_transfers([], frozenset(), NOW) == CustodyFlow(0, {})
    assert summarize_transfers([], GROUP, NOW) == CustodyFlow(
        0,
        {"0xaaa": 0, "0xbbb": 0},
    )


@pytest.mark.parametrize(
    ("transfer", "message"),
    [
        (Transfer("0xaaa", "0xoutside", -1, NOW), "amount"),
        (Transfer("0xaaa", "0xoutside", float("nan"), NOW), "finite"),
        (Transfer("0xaaa", "0xoutside", float("inf"), NOW), "finite"),
        (Transfer("0xaaa", "0xoutside", float("-inf"), NOW), "finite"),
        (
            Transfer("0xaaa", "0xoutside", 1, NOW + timedelta(microseconds=1)),
            "future",
        ),
    ],
)
def test_invalid_transfer_data_is_rejected(
    transfer: Transfer,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        summarize_transfers([transfer], GROUP, NOW)


def test_transfer_timestamps_and_now_must_be_timezone_aware() -> None:
    naive_now = NOW.replace(tzinfo=None)
    naive_transfer = Transfer("0xaaa", "0xoutside", 1, naive_now)

    with pytest.raises(ValueError, match="now.*timezone-aware"):
        summarize_transfers([], GROUP, naive_now)
    with pytest.raises(ValueError, match="observed_at.*timezone-aware"):
        summarize_transfers([naive_transfer], GROUP, NOW)


@pytest.mark.parametrize(
    ("share", "expected"),
    [
        (0.50, RiskLevel.GREEN),
        (0.500001, RiskLevel.YELLOW),
        (0.70, RiskLevel.YELLOW),
        (0.700001, RiskLevel.RED),
    ],
)
def test_concentration_uses_strict_thresholds(
    share: float,
    expected: RiskLevel,
) -> None:
    result = evaluate_concentration(share, RiskLevel.GREEN, 0)

    assert result == RuleDecision(expected, 0)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_concentration_rejects_non_finite_share(value: float) -> None:
    with pytest.raises(ValueError, match="share.*finite"):
        evaluate_concentration(value, RiskLevel.GREEN, 0)


@pytest.mark.parametrize("field", ["yellow", "red"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_concentration_rejects_non_finite_thresholds(
    field: str,
    value: float,
) -> None:
    kwargs = {field: value}

    with pytest.raises(ValueError, match=f"{field}.*finite"):
        evaluate_concentration(0.60, RiskLevel.GREEN, 0, **kwargs)


@pytest.mark.parametrize("value", [50_000_000, -50_000_000])
def test_entity_flow_threshold_is_strict(value: float) -> None:
    assert evaluate_entity_flow(value, RiskLevel.GREEN, 0) == RuleDecision(
        RiskLevel.GREEN,
        0,
    )


@pytest.mark.parametrize("value", [50_000_001, -50_000_001])
def test_entity_flow_warning_never_raises_red(value: float) -> None:
    assert evaluate_entity_flow(value, RiskLevel.RED, 1) == RuleDecision(
        RiskLevel.YELLOW,
        0,
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_entity_flow_rejects_non_finite_value(value: float) -> None:
    with pytest.raises(ValueError, match="value.*finite"):
        evaluate_entity_flow(value, RiskLevel.GREEN, 0)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_entity_flow_rejects_non_finite_threshold(value: float) -> None:
    with pytest.raises(ValueError, match="threshold.*finite"):
        evaluate_entity_flow(1, RiskLevel.GREEN, 0, threshold=value)


def test_address_outflow_threshold_is_strict_and_empty_is_clear() -> None:
    assert evaluate_address_outflow({}, RiskLevel.GREEN, 0) == RuleDecision(
        RiskLevel.GREEN,
        0,
    )
    assert evaluate_address_outflow(
        {"0xaaa": 100_000_000},
        RiskLevel.GREEN,
        0,
    ) == RuleDecision(RiskLevel.GREEN, 0)


def test_address_outflow_warning_never_raises_red() -> None:
    assert evaluate_address_outflow(
        {"0xaaa": 100_000_001, "0xbbb": 1},
        RiskLevel.RED,
        1,
    ) == RuleDecision(RiskLevel.YELLOW, 0)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_address_outflow_rejects_non_finite_values(value: float) -> None:
    with pytest.raises(ValueError, match="values.*finite"):
        evaluate_address_outflow({"0xaaa": value}, RiskLevel.GREEN, 0)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_address_outflow_rejects_non_finite_threshold(value: float) -> None:
    with pytest.raises(ValueError, match="threshold.*finite"):
        evaluate_address_outflow(
            {"0xaaa": 1},
            RiskLevel.GREEN,
            0,
            threshold=value,
        )


@pytest.mark.parametrize(
    ("evaluate", "clear_value", "trigger_value"),
    [
        (evaluate_concentration, 0.10, 0.60),
        (evaluate_entity_flow, 0, 50_000_001),
        (evaluate_address_outflow, {}, {"0xaaa": 100_000_001}),
    ],
)
def test_warning_recovers_after_two_clear_checks_and_retrigger_resets_progress(
    evaluate: object,
    clear_value: object,
    trigger_value: object,
) -> None:
    evaluator = evaluate  # keep each parametrized call easy to read below
    first = evaluator(clear_value, RiskLevel.YELLOW, 0)  # type: ignore[operator]
    retriggered = evaluator(  # type: ignore[operator]
        trigger_value,
        first.level,
        first.clear_checks,
    )
    clear_again = evaluator(  # type: ignore[operator]
        clear_value,
        retriggered.level,
        retriggered.clear_checks,
    )
    recovered = evaluator(  # type: ignore[operator]
        clear_value,
        clear_again.level,
        clear_again.clear_checks,
    )

    assert first == RuleDecision(RiskLevel.YELLOW, 1)
    assert retriggered == RuleDecision(RiskLevel.YELLOW, 0)
    assert clear_again == RuleDecision(RiskLevel.YELLOW, 1)
    assert recovered == RuleDecision(RiskLevel.GREEN, 0)


def test_custom_recovery_check_count_is_supported() -> None:
    first = evaluate_concentration(
        0.10,
        RiskLevel.RED,
        0,
        recovery_checks=3,
    )
    second = evaluate_concentration(
        0.10,
        first.level,
        first.clear_checks,
        recovery_checks=3,
    )
    third = evaluate_concentration(
        0.10,
        second.level,
        second.clear_checks,
        recovery_checks=3,
    )

    assert first == RuleDecision(RiskLevel.RED, 1)
    assert second == RuleDecision(RiskLevel.RED, 2)
    assert third == RuleDecision(RiskLevel.GREEN, 0)


@pytest.mark.parametrize(
    ("clear_checks", "recovery_checks", "message"),
    [
        (-1, 2, "clear_checks"),
        (0, 0, "recovery_checks"),
        (0, -1, "recovery_checks"),
    ],
)
def test_recovery_counters_must_be_valid(
    clear_checks: int,
    recovery_checks: int,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        evaluate_concentration(
            0.10,
            RiskLevel.GREEN,
            clear_checks,
            recovery_checks=recovery_checks,
        )
