from pathlib import Path

import pytest

from usd1_monitor.collectors import attestations
from usd1_monitor.collectors.attestations import (
    AttestationParseError,
    extract_attestation,
    extract_attestation_text,
    extract_pdf_text,
)


FIXTURES = Path(__file__).parent / "fixtures"


def test_extract_attestation_fields() -> None:
    report = extract_attestation_text(
        (FIXTURES / "usd1_attestation.txt").read_text(encoding="utf-8")
    )

    assert report.report_month == "2026-07"
    assert report.tokens_outstanding > 0
    assert report.redemption_assets >= report.tokens_outstanding
    assert report.auditor
    assert report.custodian
    assert "cash equivalents" in {
        value.casefold() for value in report.asset_categories
    }


def test_changed_whitespace_layout_still_parses() -> None:
    text = (FIXTURES / "usd1_attestation.txt").read_text(encoding="utf-8")
    report = extract_attestation_text("\n\n  " + text.replace(": ", ":\n") + "  ")
    assert report.report_month == "2026-07"


def test_missing_total_names_the_missing_field() -> None:
    text = (FIXTURES / "usd1_attestation.txt").read_text(encoding="utf-8")
    text = text.replace("Redemption Assets: 4,250,000,000", "")

    with pytest.raises(AttestationParseError, match="redemption_assets"):
        extract_attestation_text(text)


def test_pdf_pages_are_joined_in_order(monkeypatch) -> None:
    text = (FIXTURES / "usd1_attestation.txt").read_text(encoding="utf-8")
    split = text.index("Redemption Assets")

    class Page:
        def __init__(self, value: str) -> None:
            self.value = value

        def extract_text(self) -> str:
            return self.value

    class Reader:
        def __init__(self, stream) -> None:
            self.pages = [Page(text[:split]), Page(text[split:])]

    monkeypatch.setattr(attestations, "PdfReader", Reader)
    report = extract_attestation(b"fake pdf")
    assert report.tokens_outstanding == 4_200_000_000


def test_pdf_page_count_is_bounded(monkeypatch) -> None:
    class Reader:
        def __init__(self, stream) -> None:
            self.pages = [object()] * 101

    monkeypatch.setattr(attestations, "PdfReader", Reader)

    with pytest.raises(AttestationParseError, match="more than 100 pages"):
        extract_pdf_text(b"fake pdf")


def test_current_bitgo_report_layout_is_parsed() -> None:
    text = """
    Crowe LLP Independent Accountant's Examination Report
    BitGo Bank & Trust, N.A.
    Summary March 18, 2026 March 31, 2026
    Total USD1 redeemable tokens outstanding (see Note A)
    4,506,314,008 4,392,802,478
    Total redemption assets available for USD1 redeemable tokens outstanding (see Note B)
    $4,507,840,781 $4,392,828,274
    Note B: Redemption assets may include cash, cash equivalents,
    short-term U.S. Treasuries, reverse repo agreements fully collateralized
    by U.S. Treasuries and Government Money Market funds.
    """

    report = extract_attestation_text(text)

    assert report.report_month == "2026-03"
    assert report.tokens_outstanding == 4_392_802_478
    assert report.redemption_assets == 4_392_828_274
    assert report.auditor == "Crowe LLP"
    assert report.custodian == "BitGo Bank & Trust, N.A."
    assert "Government Money Market funds" in report.asset_categories


def test_current_layout_skips_narrative_heading_without_numbers() -> None:
    text = """
    Crowe LLP Independent Accountant's Examination Report
    BitGo Bank & Trust, N.A.
    We examined Total USD1 redeemable tokens outstanding and
    Total redemption assets under the applicable criteria.
    Summary June 30, 2026 July 31, 2026
    Total USD1 redeemable tokens outstanding (see Note A)
    4,450,000,000 4,420,000,000
    Total redemption assets available for USD1 redeemable tokens outstanding (see Note B)
    $4,451,000,000 $4,421,000,000
    Note B: Redemption assets may include cash and cash equivalents,
    short-term U.S. Treasuries, reverse repo agreements and
    Government Money Market funds.
    """

    report = extract_attestation_text(text)

    assert report.report_month == "2026-07"
    assert report.tokens_outstanding == 4_420_000_000
    assert report.redemption_assets == 4_421_000_000
