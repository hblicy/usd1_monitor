from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from datetime import datetime

from pypdf import PdfReader

class AttestationParseError(ValueError):
    pass


@dataclass(frozen=True)
class AttestationReport:
    report_month: str
    tokens_outstanding: float
    redemption_assets: float
    asset_categories: tuple[str, ...]
    auditor: str
    custodian: str


def pdf_sha256(pdf_bytes: bytes) -> str:
    return hashlib.sha256(pdf_bytes).hexdigest()


def extract_pdf_text(pdf_bytes: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        if len(reader.pages) > 100:
            raise AttestationParseError("PDF has more than 100 pages")
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except AttestationParseError:
        raise
    except Exception as exc:
        raise AttestationParseError(f"unable to read PDF: {exc}") from exc


def _number(pattern: str, text: str) -> float | None:
    match = re.search(pattern, text, re.IGNORECASE)
    if match is None:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _last_large_number_between(
    text: str, start_pattern: str, end_pattern: str
) -> float | None:
    matches = re.finditer(
        start_pattern + r"(.+?)" + end_pattern,
        text,
        re.IGNORECASE,
    )
    result = None
    for match in matches:
        values = re.findall(r"\$?([0-9][0-9,]{3,})", match.group(1))
        if values:
            result = float(values[-1].replace(",", ""))
    return result


def extract_attestation_text(text: str) -> AttestationReport:
    canonical = re.sub(r"\s+", " ", text).strip()
    month_match = re.search(
        r"Report\s+Month\s*:\s*([A-Za-z]+)\s+(20\d{2})",
        canonical,
        re.IGNORECASE,
    )
    report_month = None
    if month_match:
        try:
            parsed = datetime.strptime(
                f"{month_match.group(1)} {month_match.group(2)}", "%B %Y"
            )
            report_month = parsed.strftime("%Y-%m")
        except ValueError:
            report_month = None
    if report_month is None:
        report_dates = re.findall(
            r"([A-Za-z]+)\s+\d{1,2},\s*(20\d{2})", canonical
        )
        for month_name, year in reversed(report_dates):
            try:
                report_month = datetime.strptime(
                    f"{month_name} {year}", "%B %Y"
                ).strftime("%Y-%m")
                break
            except ValueError:
                continue
    tokens = _number(
        r"Tokens\s+Outstanding\s*:\s*([0-9][0-9,]*(?:\.\d+)?)",
        canonical,
    )
    assets = _number(
        r"Redemption\s+Assets\s*:\s*([0-9][0-9,]*(?:\.\d+)?)",
        canonical,
    )
    if tokens is None:
        tokens = _last_large_number_between(
            canonical,
            r"Total\s+USD1\s+redeemable\s+tokens\s+outstanding",
            r"Total\s+redemption\s+assets",
        )
    if assets is None:
        assets = _last_large_number_between(
            canonical,
            r"Total\s+redemption\s+assets.+?outstanding(?:\s*\(see\s+Note\s+B\))?",
            r"(?:Comparison\s+of|Note\s+B:)",
        )
    categories_match = re.search(
        r"Asset\s+Categories\s*:\s*(.+?)\s+Auditor\s*:",
        canonical,
        re.IGNORECASE,
    )
    auditor_match = re.search(
        r"Auditor\s*:\s*(.+?)\s+Custodian\s*:", canonical, re.IGNORECASE
    )
    custodian_match = re.search(
        r"Custodian\s*:\s*(.+)$", canonical, re.IGNORECASE
    )
    categories = (
        tuple(
            value.strip()
            for value in re.split(r"[;|]", categories_match.group(1))
            if value.strip()
        )
        if categories_match
        else ()
    )
    auditor = auditor_match.group(1).strip() if auditor_match else ""
    custodian = custodian_match.group(1).strip() if custodian_match else ""
    if not categories:
        known_categories = (
            "Cash and cash equivalents",
            "short-term U.S. Treasuries",
            "reverse repo agreements",
            "Government Money Market funds",
        )
        categories = tuple(
            category
            for category in known_categories
            if category.casefold() in canonical.casefold()
        )
    if not auditor:
        match = re.search(r"\b([A-Z][A-Za-z& ]+\sLLP)\b", canonical)
        auditor = match.group(1).strip() if match else ""
    if not custodian:
        match = re.search(r"\b(BitGo\s+Bank\s*&\s*Trust,\s*N\.A\.)", canonical)
        custodian = match.group(1) if match else ""
    fields = {
        "report_month": report_month,
        "tokens_outstanding": tokens,
        "redemption_assets": assets,
        "asset_categories": categories,
        "auditor": auditor,
        "custodian": custodian,
    }
    missing = [name for name, value in fields.items() if not value]
    if missing:
        raise AttestationParseError(
            f"missing required attestation fields: {', '.join(missing)}"
        )
    return AttestationReport(
        report_month=report_month,
        tokens_outstanding=tokens,
        redemption_assets=assets,
        asset_categories=categories,
        auditor=auditor,
        custodian=custodian,
    )


def extract_attestation(pdf_bytes: bytes) -> AttestationReport:
    pdf_sha256(pdf_bytes)
    text = extract_pdf_text(pdf_bytes)
    return extract_attestation_text(text)
