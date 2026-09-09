from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta

from usd1_monitor.collectors.announcements import matches_usd1, normalize_text
from usd1_monitor.models import RiskLevel


@dataclass(frozen=True)
class InformationClassification:
    level: RiskLevel
    evidence: str


RISK_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bdelist(?:s|ed|ing)?\b",
        r"\bsuspend(?:s|ed|ing)?\b",
        r"\brestrict(?:s|ed|ing|ion|ions)?\b",
        r"\binvestigat(?:e|es|ed|ing|ion|ions)\b",
        r"\bfreez(?:e|es|ing)\b|\bfrozen\b",
        r"下架|暂停|限制|调查|冻结",
    )
)
_ENGLISH_NEGATION_BEFORE = re.compile(
    r"(?:\b(?:not|never|no\s+longer)\b|"
    r"\b(?:won|isn|aren|wasn|weren|don|doesn|didn|can|couldn|"
    r"shouldn|wouldn|mustn)['’]t\b)"
    r"(?:\W+\w+){0,4}\W*$",
    re.IGNORECASE,
)
_CHINESE_NEGATION_BEFORE = re.compile(
    r"(?:不会|并未|没有|未曾|未被|不再|不|未|取消)(?:被|受到)?$"
)
_DIRECT_ABSENCE_BEFORE = re.compile(
    r"(?:\b(?:no|without)\b|无)\W*$",
    re.IGNORECASE,
)
_TIME_ADVERB = r"(?:currently|presently|now)"
_NON_APPLICABLE_AFTER = re.compile(
    rf"^\W*(?:{_TIME_ADVERB}\s+)?"
    rf"(?:(?:do|does|did|will|would|is|are|was|were)\s+"
    rf"(?:{_TIME_ADVERB}\s+)?not|"
    rf"(?:doesn|don|didn|won|isn|aren|wasn|weren)['’]t|"
    rf"(?:is|are|was|were)\s+(?:{_TIME_ADVERB}\s+)?no\s+longer|"
    r"no\s+longer)"
    rf"\s+(?:{_TIME_ADVERB}\s+)?(?:apply|applicable)\b",
    re.IGNORECASE,
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
    clauses = (
        clause.strip()
        for clause in re.split(r"[.!?;。！？；]+", canonical)
    )
    for clause in clauses:
        if not clause or not matches_usd1(clause):
            continue
        risk_matches = sorted(
            (
                match
                for pattern in RISK_PATTERNS
                for match in pattern.finditer(clause)
            ),
            key=lambda match: match.start(),
        )
        scope_start = 0
        for index, match in enumerate(risk_matches):
            next_start = (
                risk_matches[index + 1].start()
                if index + 1 < len(risk_matches)
                else len(clause)
            )
            prefix = clause[scope_start:match.start()]
            suffix = clause[match.end():next_start]
            non_applicable = _NON_APPLICABLE_AFTER.search(suffix)
            if non_applicable is not None:
                scope_start = match.end() + non_applicable.end()
                continue
            if _ENGLISH_NEGATION_BEFORE.search(prefix):
                scope_start = match.end()
                continue
            if _CHINESE_NEGATION_BEFORE.search(prefix):
                scope_start = match.end()
                continue
            if _DIRECT_ABSENCE_BEFORE.search(prefix):
                scope_start = match.end()
                continue
            return InformationClassification(RiskLevel.YELLOW, clause)
    return InformationClassification(RiskLevel.GREEN, canonical)
