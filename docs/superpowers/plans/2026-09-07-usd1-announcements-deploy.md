# USD1 Official Information and Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the USD1 MVP with official-page change detection, attestation extraction and lateness rules, unified scheduling, Linux systemd deployment, operational documentation, and full verification.

**Architecture:** Source-specific adapters convert official pages and PDFs into a stable announcement model. Content hashes and stable IDs prevent duplicates; parsing failures feed collector-health rules. The completed single-process scheduler runs market, EVM, reserve, supply, and official-information jobs independently.

**Tech Stack:** Python 3.11+, existing monitor stack, Beautiful Soup 4, pypdf, pytest, systemd

---

Complete the core-market, EVM, and reserves-supply plans first. Run commands from `07-web3-bot/DEFI/usd1_monitor`.

### Task 1: Normalize and persist official announcements

**Files:**
- Modify: `requirements.txt`
- Modify: `usd1_monitor/models.py`
- Modify: `usd1_monitor/storage.py`
- Create: `usd1_monitor/collectors/announcements.py`
- Create: `tests/test_announcements.py`

- [ ] **Step 1: Write failing de-duplication and filtering tests**

```python
# tests/test_announcements.py
from datetime import UTC, datetime

import pytest

from usd1_monitor.collectors.announcements import Announcement, matches_usd1, normalize_text


def test_world_liberty_usd1_matches() -> None:
    assert matches_usd1("Binance changes World Liberty Financial USD (USD1) margin support") is True


def test_unitas_usd1_is_excluded() -> None:
    assert matches_usd1("Unitas USD1 launches on a new network") is False


def test_normalize_text_removes_layout_noise() -> None:
    assert normalize_text("  USD1\n\n reserve\t update  ") == "USD1 reserve update"


@pytest.mark.asyncio
async def test_same_id_and_hash_is_not_inserted_twice(storage) -> None:
    item = Announcement(
        source="bitgo",
        stable_id="2026-07",
        title="USD1 July 2026 attestation",
        url="https://www.bitgo.com/usd1/attestations/",
        published_at=None,
        body_hash="abc",
        first_seen_at=datetime(2026, 9, 7, tzinfo=UTC),
    )
    assert await storage.upsert_announcement(item) == "NEW"
    assert await storage.upsert_announcement(item) == "UNCHANGED"
```

- [ ] **Step 2: Run and verify failure**

Run: `python -m pytest tests/test_announcements.py -v`

Expected: announcement interfaces do not exist.

- [ ] **Step 3: Add parser dependencies**

Append:

```text
beautifulsoup4>=4.12,<5
pypdf>=5,<7
```

- [ ] **Step 4: Implement the stable model and text filter**

```python
# public interfaces in usd1_monitor/collectors/announcements.py
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Announcement:
    source: str
    stable_id: str
    title: str
    url: str
    published_at: datetime | None
    body_hash: str
    first_seen_at: datetime


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def matches_usd1(value: str) -> bool:
    lowered = normalize_text(value).casefold()
    if "unitas" in lowered:
        return False
    entities = ("usd1", "world liberty financial", "world liberty", "bitgo")
    return any(entity in lowered for entity in entities)


def content_hash(title: str, body: str) -> str:
    canonical = normalize_text(title) + "\n" + normalize_text(body)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
```

- [ ] **Step 5: Implement announcement storage outcomes**

Create a unique key `(source, stable_id)`. `upsert_announcement()` returns `NEW`, `CHANGED`, or `UNCHANGED`; update the hash only after the new content is safely stored. Record first-seen and last-seen separately. A changed item must retain its original first-seen time.

- [ ] **Step 6: Run and commit**

Run: `python -m pytest tests/test_announcements.py tests/test_storage.py -v`

Expected: all tests pass.

```bash
git add 07-web3-bot/DEFI/usd1_monitor/requirements.txt 07-web3-bot/DEFI/usd1_monitor/usd1_monitor/models.py 07-web3-bot/DEFI/usd1_monitor/usd1_monitor/storage.py 07-web3-bot/DEFI/usd1_monitor/usd1_monitor/collectors/announcements.py 07-web3-bot/DEFI/usd1_monitor/tests/test_announcements.py
git commit -m "功能：持久化 USD1 官方信息变化"
```

### Task 2: Add official source adapters and attestation extraction

**Files:**
- Modify: `usd1_monitor/collectors/announcements.py`
- Create: `usd1_monitor/collectors/attestations.py`
- Create: `tests/fixtures/binance_announcements.html`
- Create: `tests/fixtures/bitgo_attestations.html`
- Create: `tests/fixtures/wlfi_por.html`
- Create: `tests/fixtures/occ_decisions.html`
- Create: `tests/fixtures/usd1_attestation.txt`
- Create: `tests/test_official_sources.py`
- Create: `tests/test_attestations.py`

- [ ] **Step 1: Write failing official-page parser tests**

```python
# tests/test_official_sources.py
from pathlib import Path

import pytest

from usd1_monitor.collectors.announcements import PageStructureError, parse_binance_items


FIXTURES = Path(__file__).parent / "fixtures"


def test_binance_parser_returns_only_usd1_items() -> None:
    html = (FIXTURES / "binance_announcements.html").read_text(encoding="utf-8")
    items = parse_binance_items(html, "https://www.binance.com")
    assert [item.stable_id for item in items] == ["3dd432ca980c469389d18f2f69c9d4ae"]


def test_empty_official_page_is_parser_failure() -> None:
    with pytest.raises(PageStructureError, match="no announcement entries"):
        parse_binance_items("<html><body></body></html>", "https://www.binance.com")
```

- [ ] **Step 2: Write failing attestation extraction tests**

```python
# tests/test_attestations.py
from pathlib import Path

from usd1_monitor.collectors.attestations import extract_attestation_text


def test_extract_attestation_fields() -> None:
    report = extract_attestation_text((Path(__file__).parent / "fixtures" / "usd1_attestation.txt").read_text(encoding="utf-8"))
    assert report.report_month == "2026-07"
    assert report.tokens_outstanding > 0
    assert report.redemption_assets >= report.tokens_outstanding
    assert report.auditor
    assert report.custodian
    assert "cash equivalents" in {value.casefold() for value in report.asset_categories}
```

- [ ] **Step 3: Run and verify failure**

Run: `python -m pytest tests/test_official_sources.py tests/test_attestations.py -v`

Expected: parser functions and attestation module do not exist.

- [ ] **Step 4: Implement allowlisted source adapters**

Use `BeautifulSoup` to extract canonical links and visible titles. Each adapter must enforce its host:

```python
ALLOWED_HOSTS = {
    "binance": {"www.binance.com", "binance.com"},
    "bitgo": {"www.bitgo.com", "bitgo.com", "landing.bitgo.com"},
    "wlfi": {"docs.worldlibertyfinancial.com", "worldlibertyfinancial.com"},
    "occ": {"www.occ.gov", "occ.gov"},
}
```

Reject redirects or extracted links outside the source's allowlist. Stable IDs are Binance detail IDs, attestation report month plus PDF URL hash, WLFI canonical path, and OCC decision number. If the page yields no structural entries, raise `PageStructureError` so health logic can alert.

- [ ] **Step 5: Implement PDF extraction with explicit failure**

```python
# public result in usd1_monitor/collectors/attestations.py
from dataclasses import dataclass


@dataclass(frozen=True)
class AttestationReport:
    report_month: str
    tokens_outstanding: float
    redemption_assets: float
    asset_categories: tuple[str, ...]
    auditor: str
    custodian: str
```

`extract_attestation(pdf_bytes)` reads pages with `pypdf.PdfReader` and passes their joined text to `extract_attestation_text(text)`. The text parser normalizes whitespace and uses labeled field patterns rather than fixed line positions. Require all dataclass fields. Missing fields raise `AttestationParseError` containing the missing field names, never zero/empty defaults. Compute and persist the PDF SHA-256 before extraction. Unit tests use the committed text fixture; a separate test monkeypatches `PdfReader` with two in-memory fake pages and verifies their text is joined in page order.

- [ ] **Step 6: Test changed layouts and cross-domain links**

Add tests proving a valid changed whitespace layout still parses, a missing total fails, an external PDF host is rejected, and a modified body hash returns `CHANGED` once.

Run: `python -m pytest tests/test_official_sources.py tests/test_attestations.py tests/test_announcements.py -v`

Expected: all tests pass.

- [ ] **Step 7: Commit official adapters**

```bash
git add 07-web3-bot/DEFI/usd1_monitor/usd1_monitor/collectors/announcements.py 07-web3-bot/DEFI/usd1_monitor/usd1_monitor/collectors/attestations.py 07-web3-bot/DEFI/usd1_monitor/tests/fixtures/binance_announcements.html 07-web3-bot/DEFI/usd1_monitor/tests/fixtures/bitgo_attestations.html 07-web3-bot/DEFI/usd1_monitor/tests/fixtures/wlfi_por.html 07-web3-bot/DEFI/usd1_monitor/tests/fixtures/occ_decisions.html 07-web3-bot/DEFI/usd1_monitor/tests/fixtures/usd1_attestation.txt 07-web3-bot/DEFI/usd1_monitor/tests/test_official_sources.py 07-web3-bot/DEFI/usd1_monitor/tests/test_attestations.py
git commit -m "功能：解析 USD1 官方公告与鉴证"
```

### Task 3: Implement information risk rules and independent scheduling

**Files:**
- Create: `usd1_monitor/engine/information_rules.py`
- Create: `tests/test_information_rules.py`
- Create: `tests/test_information_integration.py`
- Modify: `usd1_monitor/config.py`
- Modify: `config.example.yaml`
- Modify: `usd1_monitor/scheduler.py`
- Modify: `usd1_monitor/cli.py`

- [ ] **Step 1: Write failing lateness and severity tests**

```python
# tests/test_information_rules.py
from datetime import UTC, date, datetime

from usd1_monitor.engine.information_rules import attestation_due_at, classify_official_text
from usd1_monitor.models import RiskLevel


def test_july_report_due_date_is_forty_five_days_after_month_end() -> None:
    assert attestation_due_at("2026-07") == date(2026, 9, 14)


def test_official_suspension_text_is_yellow_not_red() -> None:
    result = classify_official_text("Binance suspends USD1 deposits and withdrawals")
    assert result.level is RiskLevel.YELLOW
    assert "suspends" in result.evidence.casefold()


def test_unrelated_binance_item_is_green() -> None:
    assert classify_official_text("Binance lists an unrelated token").level is RiskLevel.GREEN
```

- [ ] **Step 2: Run and verify failure**

Run: `python -m pytest tests/test_information_rules.py -v`

Expected: information rule module does not exist.

- [ ] **Step 3: Implement exact information rules**

`attestation_due_at("YYYY-MM")` returns the report month's final calendar day plus 45 days. A missing due report is yellow. A new auditor, custodian, asset category, or an extraction failure is yellow. Match configurable English and Chinese words for delist, suspend, restrict, reserve, custody, charter, investigation, freeze, and attestation; official text remains at most yellow. An unchanged item is green and produces no transition.

- [ ] **Step 4: Schedule sources independently**

Run Binance every 15 minutes and BitGo/WLFI/OCC every 6 hours. Assign separate health keys so a Binance layout failure does not suppress OCC or BitGo. Persist items before evaluating them. Notify only for `NEW` or `CHANGED` matching items; include the official URL and detected change type.

- [ ] **Step 5: Extend status output and test isolation**

`status` shows latest successful poll and newest matching item for each source, latest published attestation month, next due date, and current information risk. Integration tests must prove a broken Binance parser still stores a new OCC decision and generates Binance health risk only.

Run: `python -m pytest tests/test_information_rules.py tests/test_information_integration.py -v`

Expected: all tests pass.

- [ ] **Step 6: Commit information monitoring**

```bash
git add 07-web3-bot/DEFI/usd1_monitor/usd1_monitor/engine/information_rules.py 07-web3-bot/DEFI/usd1_monitor/usd1_monitor/config.py 07-web3-bot/DEFI/usd1_monitor/config.example.yaml 07-web3-bot/DEFI/usd1_monitor/usd1_monitor/scheduler.py 07-web3-bot/DEFI/usd1_monitor/usd1_monitor/cli.py 07-web3-bot/DEFI/usd1_monitor/tests/test_information_rules.py 07-web3-bot/DEFI/usd1_monitor/tests/test_information_integration.py
git commit -m "功能：接入 USD1 官方信息风险告警"
```

### Task 4: Add Linux deployment, operational documentation, and scheme corrections

**Files:**
- Create: `deploy/usd1-monitor.service`
- Create: `README.md`
- Modify: `方案.md`
- Create: `tests/test_examples.py`
- Create: `tests/test_status_output.py`

- [ ] **Step 1: Write failing example-config and status tests**

```python
# tests/test_examples.py
from pathlib import Path

from usd1_monitor.config import load_config


def test_example_config_is_loadable() -> None:
    config = load_config(Path("config.example.yaml"), environ={})
    assert set(config.chains) == {"ethereum", "bsc"}
    assert config.por.interval_seconds == 300
```

```python
# tests/test_status_output.py
from usd1_monitor.cli import NOT_MONITORED


def test_status_names_every_non_monitored_capability() -> None:
    expected = {
        "private_exchange_account",
        "active_conversion_probe",
        "tron_solana_aptos_bridges",
        "binance_wallet_concentration",
        "social_media_sentiment",
        "defi_liquidations",
        "web_dashboard",
    }
    assert set(NOT_MONITORED) == expected
```

- [ ] **Step 2: Run and verify failure**

Run: `python -m pytest tests/test_examples.py tests/test_status_output.py -v`

Expected: the final example configuration/status coverage is incomplete.

- [ ] **Step 3: Create the systemd unit**

```ini
# deploy/usd1-monitor.service
[Unit]
Description=USD1 Public Risk Monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=usd1-monitor
Group=usd1-monitor
WorkingDirectory=/opt/usd1-monitor
EnvironmentFile=/etc/usd1-monitor.env
ExecStart=/opt/usd1-monitor/.venv/bin/python -m usd1_monitor --config /etc/usd1-monitor.yaml run
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/usd1-monitor /var/log/usd1-monitor

[Install]
WantedBy=multi-user.target
```

Set the production example database path to `/var/lib/usd1-monitor/monitor.db` and log path to `/var/log/usd1-monitor/monitor.log`; document that local development should copy and adjust `config.example.yaml`.

- [ ] **Step 4: Write exact operator instructions**

README sections must include Python environment creation, dependency install, config copy, `.env` permissions, `check`, `run`, `status`, systemd install/start/status/journal commands, database backup, log locations, alert semantics, public data sources, `ESTIMATED` meaning, all `NOT_MONITORED` items, and troubleshooting for Binance region blocking, RPC failures, parser failures, and missing webhook.

- [ ] **Step 5: Correct the original scheme without changing its investment rules**

Apply the nine approved corrections from the design spec: make July-2026 attestation a dated historical note, make chain count dynamic, separate Oracle/official/estimated coverage, generalize privileged-call monitoring beyond `drain/reallocate` names, mark private exchange and concentration features as phase two, keep political signals manual, make campaign dates dynamic, add monitor-health/reorg/notification-failure rules, and introduce `NOT_MONITORED`. Do not alter the user's stated position-management thresholds.

- [ ] **Step 6: Run focused documentation/config tests**

Run: `python -m pytest tests/test_examples.py tests/test_status_output.py -v`

Expected: both files pass.

- [ ] **Step 7: Commit deployment and documentation**

```bash
git add 07-web3-bot/DEFI/usd1_monitor/deploy/usd1-monitor.service 07-web3-bot/DEFI/usd1_monitor/README.md 07-web3-bot/DEFI/usd1_monitor/方案.md 07-web3-bot/DEFI/usd1_monitor/tests/test_examples.py 07-web3-bot/DEFI/usd1_monitor/tests/test_status_output.py
git commit -m "文档：补充 USD1 监控部署与风险边界"
```

### Task 5: Perform full offline and read-only live verification

**Files:**
- Create: `tests/live/test_public_sources.py`
- Create: `tests/live/conftest.py`
- Modify: `pytest.ini`
- Modify: `README.md`

- [ ] **Step 1: Add opt-in live-test gating**

```python
# tests/live/conftest.py
import os

import pytest


def pytest_collection_modifyitems(config, items) -> None:
    if os.getenv("USD1_RUN_LIVE_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="set USD1_RUN_LIVE_TESTS=1 to run public read-only checks")
    for item in items:
        if "tests/live" in str(item.fspath).replace("\\", "/"):
            item.add_marker(skip)
```

- [ ] **Step 2: Add non-mutating public-source assertions**

Live tests call the same collectors as `check` and assert schema/invariants only: Binance symbols return explicit status; books are nonempty when trading; EVM chain IDs match 1 and 56; token code is nonempty; PoR timestamp is positive and not far in the future; supplies are positive; DefiLlama identity exactly matches World Liberty Financial USD; each official page yields structural entries. No fixed price, supply, report month, activity date, or implementation address is asserted.

- [ ] **Step 3: Run the complete offline suite**

Run: `python -m pytest -v`

Expected: all offline tests pass; live tests are reported skipped; no network or webhook call occurs.

- [ ] **Step 4: Run read-only live verification**

Run: `$env:USD1_RUN_LIVE_TESTS='1'; python -m pytest tests/live -v`

Expected: all reachable public-source tests pass. A region-blocked Binance endpoint is a failed deployment prerequisite, not a skipped success.

- [ ] **Step 5: Run the actual one-shot command with alerts disabled**

Run: `python -m usd1_monitor --config config.example.yaml check`

Expected: exit 0; every enabled collector prints a successful data timestamp; no enterprise WeChat message is sent.

- [ ] **Step 6: Inspect repository scope and commit live verification**

Run: `git status --short -- 07-web3-bot/DEFI/usd1_monitor`

Expected: only intended USD1 monitor files are changed; `.env`, `config.yaml`, database files, logs, caches, and credentials are absent.

```bash
git add 07-web3-bot/DEFI/usd1_monitor/tests/live 07-web3-bot/DEFI/usd1_monitor/pytest.ini 07-web3-bot/DEFI/usd1_monitor/README.md
git commit -m "测试：验证 USD1 公开监控数据源"
```
