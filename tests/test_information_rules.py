from datetime import date

import pytest

from usd1_monitor.engine.information_rules import (
    attestation_due_at,
    classify_official_text,
    next_attestation_due_at,
)
from usd1_monitor.models import RiskLevel


def test_july_report_due_date_is_forty_five_days_after_month_end() -> None:
    assert attestation_due_at("2026-07") == date(2026, 9, 14)


def test_next_report_after_july_is_due_after_august_month_end() -> None:
    assert next_attestation_due_at("2026-07") == date(2026, 10, 15)


def test_official_suspension_text_is_yellow_not_red() -> None:
    result = classify_official_text(
        "Binance suspends USD1 deposits and withdrawals"
    )

    assert result.level is RiskLevel.YELLOW
    assert "suspends" in result.evidence.casefold()


def test_chinese_risk_word_is_detected() -> None:
    assert classify_official_text("币安暂停 USD1 充值").level is RiskLevel.YELLOW


def test_unrelated_binance_item_is_green() -> None:
    assert (
        classify_official_text("Binance lists an unrelated token").level
        is RiskLevel.GREEN
    )


def test_wlfi_listing_boilerplate_is_not_usd1_risk() -> None:
    result = classify_official_text(
        "Binance will list World Liberty Financial (WLFI). "
        "The project is launching the USD1 stablecoin and USD1 ecosystem. "
        "Trading is unavailable in restricted countries."
    )

    assert result.level is RiskLevel.GREEN


def test_usd1_earn_promotion_terms_are_not_usd1_risk() -> None:
    result = classify_official_text(
        "USD1 Flexible Products offer an 8.5% APR. "
        "Users must observe regional restrictions. "
        "Binance may suspend this Promotion."
    )

    assert result.level is RiskLevel.GREEN


def test_direct_usd1_adverse_clause_is_returned_as_evidence() -> None:
    result = classify_official_text(
        "General service update. USD1 withdrawals are suspended immediately. "
        "Binance reserves the right to amend this notice."
    )

    assert result.level is RiskLevel.YELLOW
    assert result.evidence == "USD1 withdrawals are suspended immediately"


def test_unrestricted_usd1_withdrawals_are_not_risk() -> None:
    result = classify_official_text("USD1 withdrawals are unrestricted")

    assert result.level is RiskLevel.GREEN


def test_explicitly_negated_suspension_is_not_risk() -> None:
    result = classify_official_text(
        "USD1 withdrawals will not be suspended"
    )

    assert result.level is RiskLevel.GREEN


@pytest.mark.parametrize(
    "text",
    (
        "Without notice, USD1 is suspended",
        "USD1 is without warning suspended",
        "USD1 无预警暂停提现",
    ),
)
def test_warning_absence_does_not_negate_real_risk(text: str) -> None:
    assert classify_official_text(text).level is RiskLevel.YELLOW


@pytest.mark.parametrize(
    "text",
    (
        "Restrictions do not apply to USD1",
        "USD1 won't be suspended",
        "USD1 restrictions are currently not applicable",
        "USD1 restrictions do not currently apply",
        "USD1 restrictions are not currently applicable",
        "USD1 restrictions are no longer applicable",
        "USD1 restrictions are presently no longer applicable",
    ),
)
def test_other_explicit_negations_are_not_risk(text: str) -> None:
    assert classify_official_text(text).level is RiskLevel.GREEN


@pytest.mark.parametrize(
    "text",
    (
        "Restrictions do not apply to USD1, but USD1 withdrawals are suspended",
        "USD1 deposits are not restricted, but withdrawals are suspended",
        "USD1 is not frozen, but withdrawals are suspended",
    ),
)
def test_negated_action_does_not_hide_later_real_risk(text: str) -> None:
    assert classify_official_text(text).level is RiskLevel.YELLOW


@pytest.mark.parametrize(
    "text",
    (
        "USD1 withdrawals are suspended and exceptions do not apply",
        "USD1 restrictions are active and the waiver is not applicable",
    ),
)
def test_unrelated_non_applicable_subject_does_not_hide_risk(text: str) -> None:
    assert classify_official_text(text).level is RiskLevel.YELLOW
