from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

from usd1_monitor.collectors.announcements import matches_usd1, normalize_text
from usd1_monitor.models import RiskLevel


@dataclass(frozen=True)
class InformationClassification:
    level: RiskLevel
    evidence: str


RISK_WORDS = (
    "delist",
    "suspend",
    "restrict",
    "reserve",
    "custody",
    "custodian",
    "charter",
    "investigation",
    "freeze",
    "attestation",
    "下架",
    "暂停",
    "限制",
    "储备",
    "托管",
    "牌照",
    "调查",
    "冻结",
    "鉴证",
)


def attestation_due_at(report_month: str) -> date:
    try:
        year_text, month_text = report_month.split("-", 1)
        year, month = int(year_text), int(month_text)
        last_day = calendar.monthrange(year, month)[1]
        month_end = date(year, month, last_day)
    except (ValueError, TypeError) as exc:
        raise ValueError("report_month must be YYYY-MM") from exc
    return month_end + timedelta(days=45)


def next_attestation_due_at(latest_report_month: str) -> date:
    try:
        year_text, month_text = latest_report_month.split("-", 1)
        year, month = int(year_text), int(month_text)
        date(year, month, 1)
    except (ValueError, TypeError) as exc:
        raise ValueError("latest_report_month must be YYYY-MM") from exc
    if month == 12:
        year, month = year + 1, 1
    else:
        month += 1
    return attestation_due_at(f"{year:04d}-{month:02d}")


def classify_official_text(value: str) -> InformationClassification:
    canonical = normalize_text(value)
    lowered = canonical.casefold()
    if not matches_usd1(canonical):
        return InformationClassification(RiskLevel.GREEN, canonical)
    if any(word in lowered for word in RISK_WORDS):
        return InformationClassification(RiskLevel.YELLOW, canonical)
    return InformationClassification(RiskLevel.GREEN, canonical)
