from datetime import date

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
