from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Awaitable, Callable, Protocol
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from usd1_monitor.collectors.attestations import (
    AttestationParseError,
    extract_attestation,
    extract_pdf_text,
    pdf_sha256,
)
from usd1_monitor.models import Announcement


__all__ = [
    "Announcement",
    "BinanceAnnouncementCollector",
    "BinancePartialCollectionError",
    "content_hash",
    "matches_usd1",
    "normalize_text",
]


ALLOWED_HOSTS = {
    "binance": {"www.binance.com", "binance.com"},
    "bitgo": {"www.bitgo.com", "bitgo.com", "landing.bitgo.com"},
    "wlfi": {
        "docs.worldlibertyfinancial.com",
        "por.worldlibertyfinancial.com",
        "worldlibertyfinancial.com",
    },
    "occ": {"www.occ.gov", "occ.gov"},
}
BINANCE_PAGE_SIZE = 20
BINANCE_MAX_PAGES = 20
BINANCE_BODY_SCAN_LIMIT = 8
BINANCE_BODY_RESCAN_AFTER = timedelta(hours=24)
BINANCE_DETAIL_CONCURRENCY = 8
BINANCE_DETAIL_TIMEOUT_SECONDS = 10.0
BINANCE_EARLIEST_RELEASE_AT = datetime(2017, 1, 1, tzinfo=UTC)
BINANCE_RELEASE_FUTURE_TOLERANCE = timedelta(days=1)
MAX_PDF_BYTES = 20_000_000


class PageStructureError(ValueError):
    pass


class BinancePartialCollectionError(RuntimeError):
    def __init__(
        self,
        items: list[Announcement],
        failures: list[tuple[str, Exception]],
        deferred_ids: set[str] | None = None,
    ) -> None:
        self.items = items
        self.failures = tuple(failures)
        self.deferred_ids = frozenset(deferred_ids or ())
        parts = [
            f"{stable_id}: {type(error).__name__}: {error}"
            for stable_id, error in failures
        ]
        if self.deferred_ids:
            parts.append(
                "cooldown pending: " + ", ".join(sorted(self.deferred_ids))
            )
        super().__init__(
            "Binance announcement detail failures: " + "; ".join(parts)
        )


class TextHttpClient(Protocol):
    async def get_text(self, url: str) -> str: ...

    async def get_bytes(self, url: str) -> bytes: ...


class JsonHttpClient(Protocol):
    async def get_json(
        self, url: str, params: dict[str, object] | None = None
    ) -> object: ...

    async def get_text(self, url: str) -> str: ...


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


_BODY_SECTION_TAGS = (
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "p",
    "li",
    "blockquote",
    "div",
    "section",
    "pre",
    "dt",
    "dd",
    "tr",
    "td",
    "th",
)


def _body_sections(content, fallback: str) -> list[str]:
    sections: list[str] = []
    for element in content.find_all(_BODY_SECTION_TAGS):
        if element.name in {"td", "th"} and element.find_parent("tr"):
            continue
        if element.name != "tr" and element.find(_BODY_SECTION_TAGS):
            continue
        text = normalize_text(element.get_text(" "))
        if text:
            sections.append(text)
    return sections or [fallback]


def _detail_body_content(html: str) -> tuple[str, list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    content = soup.find("main") or soup.find("article")
    if content is None:
        has_html_tag = any(
            isinstance(element.name, str)
            and re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", element.name)
            for element in soup.find_all()
        )
        has_markdown_structure = bool(
            re.search(
                r"(?m)^\s*(?:#{1,6}\s+|>\s+|(?:[-*+]|\d+[.)])\s+)",
                html,
            )
        )
        if has_html_tag and not has_markdown_structure:
            raise PageStructureError("detail page has no main/article content")
        markdown_body = re.sub(
            r"(?ms)\r?\n[ \t]*---[ \t]*\r?\n(?:[ \t]*\r?\n)*"
            r"[ \t]*# Agent Instructions[ \t]*\r?\n.*\Z",
            "",
            html,
        )
        body_text = normalize_text(markdown_body)
        if not body_text:
            raise PageStructureError("detail page has no readable text")
        sections = [
            text
            for line in markdown_body.splitlines()
            if (text := normalize_text(line))
        ]
        return body_text, sections or [body_text]
    for element in content.find_all(
        ["nav", "header", "footer", "script", "style", "noscript"]
    ):
        element.decompose()
    body_text = normalize_text(content.get_text(" "))
    if not body_text:
        raise PageStructureError("detail page has no readable main/article content")
    return body_text, _body_sections(content, body_text)


def _detail_body_text(html: str) -> str:
    body_text, _ = _detail_body_content(html)
    return body_text


def matches_usd1(value: str) -> bool:
    lowered = normalize_text(value).casefold()
    if "unitas" in lowered:
        return False
    entities = ("usd1", "world liberty financial", "world liberty", "bitgo")
    return any(entity in lowered for entity in entities)


def content_hash(title: str, body: str) -> str:
    canonical = normalize_text(title) + "\n" + normalize_text(body)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _allowed_url(source: str, base_url: str, href: str) -> str:
    url = urljoin(base_url, href)
    if not _is_allowed_https(source, url):
        raise PageStructureError(f"{source} extracted link is outside allowlist")
    return url


def _is_allowed_https(source: str, url: str) -> bool:
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and parts.hostname in ALLOWED_HOSTS[source]
        and parts.username is None
        and parts.password is None
        and port in (None, 443)
    )


def _announcement(
    source: str, stable_id: str, title: str, url: str
) -> Announcement:
    seen_at = datetime.now(UTC)
    return Announcement(
        source=source,
        stable_id=stable_id,
        title=normalize_text(title),
        url=url,
        published_at=None,
        body_hash=content_hash(title, url),
        first_seen_at=seen_at,
    )


def parse_binance_items(html: str, base_url: str) -> list[Announcement]:
    soup = BeautifulSoup(html, "html.parser")
    entries = []
    for anchor in soup.find_all("a", href=True):
        match = re.search(r"/support/announcement/detail/([A-Za-z0-9_-]+)", anchor["href"])
        if match:
            entries.append((anchor, match.group(1)))
    if not entries:
        raise PageStructureError("no announcement entries found on Binance page")
    results = []
    for anchor, stable_id in entries:
        title = normalize_text(anchor.get_text(" "))
        url = _allowed_url("binance", base_url, anchor["href"])
        if matches_usd1(title):
            results.append(_announcement("binance", stable_id, title, url))
    return results


def parse_binance_payload(
    payload: object,
    base_url: str,
    *,
    require_title_match: bool = True,
    collected_at: datetime | None = None,
) -> list[Announcement]:
    entries = _binance_entries(payload)
    release_reference = collected_at or datetime.now(UTC)

    results = []
    for article in entries:
        stable_id = article.get("code")
        title = article.get("title")
        released_ms = article.get("releaseDate")
        if not isinstance(stable_id, str) or not isinstance(title, str):
            raise PageStructureError("invalid Binance announcement entry")
        if not isinstance(released_ms, int) or isinstance(released_ms, bool):
            raise PageStructureError(
                "invalid Binance announcement releaseDate"
            )
        if require_title_match and not matches_usd1(title):
            continue
        url = _allowed_url(
            "binance",
            base_url,
            f"/en/support/announcement/detail/{stable_id}",
        )
        try:
            published_at = datetime.fromtimestamp(released_ms / 1000, tz=UTC)
        except (OSError, OverflowError, ValueError) as exc:
            raise PageStructureError(
                "invalid Binance announcement releaseDate"
            ) from exc
        if (
            published_at < BINANCE_EARLIEST_RELEASE_AT
            or published_at
            > release_reference + BINANCE_RELEASE_FUTURE_TOLERANCE
        ):
            raise PageStructureError(
                "invalid Binance announcement releaseDate"
            )
        item = _announcement("binance", stable_id, title, url)
        results.append(
            Announcement(
                item.source,
                item.stable_id,
                item.title,
                item.url,
                published_at,
                item.body_hash,
                item.first_seen_at,
            )
        )
    return results


def _binance_entries(
    payload: object, *, allow_empty: bool = False
) -> list[dict[str, object]]:
    if not isinstance(payload, dict) or payload.get("code") != "000000":
        raise PageStructureError("invalid Binance announcement response")
    data = payload.get("data")
    catalogs = data.get("catalogs") if isinstance(data, dict) else None
    if not isinstance(catalogs, list):
        raise PageStructureError("no announcement entries found in Binance response")

    entries: list[dict[str, object]] = []
    for catalog in catalogs:
        articles = catalog.get("articles") if isinstance(catalog, dict) else None
        if isinstance(articles, list):
            entries.extend(article for article in articles if isinstance(article, dict))
    if not entries and not allow_empty:
        raise PageStructureError("no announcement entries found in Binance response")
    return entries


def parse_bitgo_items(html: str, base_url: str) -> list[Announcement]:
    soup = BeautifulSoup(html, "html.parser")
    entries = []
    for anchor in soup.find_all("a", href=True):
        href = unquote(str(anchor["href"]))
        context = normalize_text(anchor.get_text(" ") + " " + href).casefold()
        if (
            urlsplit(href).path.casefold().endswith(".pdf")
            and "usd1" in context
            and "attestation" in context
        ):
            entries.append(anchor)
    if not entries:
        raise PageStructureError("no announcement entries found on BitGo page")
    results = []
    for anchor in entries:
        title = normalize_text(anchor.get_text(" "))
        url = _allowed_url("bitgo", base_url, anchor["href"])
        context = unquote(title + " " + url)
        numeric_month = re.search(r"(20\d{2})[- /](0[1-9]|1[0-2])", context)
        named_month = re.search(
            r"(January|February|March|April|May|June|July|August|September|"
            r"October|November|December)[_ /-]+(20\d{2})",
            context,
            re.IGNORECASE,
        )
        if numeric_month is not None:
            report_month = f"{numeric_month.group(1)}-{numeric_month.group(2)}"
        elif named_month is not None:
            month_number = datetime.strptime(named_month.group(1), "%B").month
            report_month = f"{named_month.group(2)}-{month_number:02d}"
        else:
            raise PageStructureError("BitGo attestation has no report month")
        suffix = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
        results.append(
            _announcement("bitgo", f"{report_month}:{suffix}", title, url)
        )
    return sorted(results, key=lambda item: item.stable_id[:7])


def parse_wlfi_items(html: str, base_url: str) -> list[Announcement]:
    soup = BeautifulSoup(html, "html.parser")
    candidates = [
        (anchor.get_text(" "), str(anchor["href"]))
        for anchor in soup.find_all("a", href=True)
    ]
    candidates.extend(
        (match.group("title"), match.group("href").strip("<>"))
        for match in re.finditer(
            r"\[(?P<title>[^\]\n]+)\]\((?P<href><?[^\s)>]+>?)\)", html
        )
    )
    entries: list[tuple[str, str]] = []
    seen_urls: set[str] = set()
    for title, href in candidates:
        url = urljoin(base_url, href)
        if (
            not matches_usd1(title + " " + href)
            or not _is_allowed_https("wlfi", url)
            or url in seen_urls
        ):
            continue
        seen_urls.add(url)
        entries.append((title, href))
    if not entries:
        raise PageStructureError("no announcement entries found on WLFI page")
    results = []
    for title, href in entries:
        url = _allowed_url("wlfi", base_url, href)
        path = urlsplit(url).path.rstrip("/") or "/"
        results.append(
            _announcement("wlfi", path, title, url)
        )
    return results


def parse_occ_items(html: str, base_url: str) -> list[Announcement]:
    soup = BeautifulSoup(html, "html.parser")
    entries = []
    for anchor in soup.find_all("a", href=True):
        match = re.search(r"cd(\d+)\.pdf", str(anchor["href"]), re.IGNORECASE)
        if match:
            entries.append((anchor, match.group(1)))
    if not entries:
        raise PageStructureError("no announcement entries found on OCC page")
    results = []
    for anchor, decision in entries:
        row = anchor.find_parent("tr")
        title = normalize_text(
            row.get_text(" ") if row is not None else anchor.get_text(" ")
        )
        url = _allowed_url("occ", base_url, anchor["href"])
        if matches_usd1(title):
            results.append(_announcement("occ", decision, title, url))
    return results


class OfficialPageCollector:
    def __init__(
        self,
        source: str,
        url: str,
        http: TextHttpClient,
        parser: Callable[[str, str], list[Announcement]],
    ) -> None:
        if source not in ALLOWED_HOSTS:
            raise ValueError(f"unsupported official source: {source}")
        if not _is_allowed_https(source, url):
            raise ValueError(f"configured {source} URL is outside allowlist")
        self.source = source
        self._url = url
        self._http = http
        self._parser = parser

    async def collect(self, collected_at: datetime) -> list[Announcement]:
        html = await self._http.get_text(self._url)
        items = self._parser(html, self._url)
        if self.source == "bitgo" and items:
            latest_month = max(item.stable_id[:7] for item in items)
            latest = [item for item in items if item.stable_id[:7] == latest_month]
            revisions = [
                item
                for item in latest
                if any(
                    marker in f"{item.title} {item.url}".casefold()
                    for marker in ("revised", "revision", "amended", "updated")
                )
            ]
            items = [(revisions or latest)[0]]
        enriched: list[Announcement] = []
        for item in items:
            if not _is_allowed_https(self.source, item.url):
                raise PageStructureError(
                    f"{self.source} detail URL is outside allowlist"
                )
            metadata: dict[str, object]
            is_pdf = urlsplit(item.url).path.casefold().endswith(".pdf")
            if self.source == "bitgo" and is_pdf:
                pdf_bytes = await self._http.get_bytes(item.url)
                if len(pdf_bytes) > MAX_PDF_BYTES:
                    raise PageStructureError("BitGo PDF exceeds size limit")
                body_hash = pdf_sha256(pdf_bytes)
                try:
                    metadata = asdict(
                        await asyncio.to_thread(extract_attestation, pdf_bytes)
                    )
                except AttestationParseError as exc:
                    metadata = {"parse_error": str(exc)}
                report_month = metadata.get("report_month")
                link_month = item.stable_id.split(":", 1)[0]
                if isinstance(report_month, str) and report_month != link_month:
                    raise PageStructureError(
                        "BitGo report month mismatch: "
                        f"link={link_month}, pdf={report_month}"
                    )
                metadata["pdf_sha256"] = body_hash
                metadata["content_version"] = "pdf-v1"
            elif is_pdf:
                pdf_bytes = await self._http.get_bytes(item.url)
                if len(pdf_bytes) > MAX_PDF_BYTES:
                    raise PageStructureError(
                        f"{self.source} PDF exceeds size limit"
                    )
                body_text = normalize_text(
                    await asyncio.to_thread(extract_pdf_text, pdf_bytes)
                )
                if not body_text:
                    raise PageStructureError(
                        f"{self.source} PDF has no readable text"
                    )
                body_hash = content_hash(item.title, body_text)
                metadata = {
                    "body_text": body_text,
                    "pdf_sha256": pdf_sha256(pdf_bytes),
                    "content_version": "pdf-v1",
                }
            else:
                detail_html = await self._http.get_text(item.url)
                body_text, body_sections = _detail_body_content(detail_html)
                body_hash = content_hash(item.title, body_text)
                metadata = {
                    "body_text": body_text,
                    "body_sections": body_sections,
                    "content_version": "body-v1",
                }
            enriched.append(
                Announcement(
                    item.source,
                    item.stable_id,
                    item.title,
                    item.url,
                    item.published_at,
                    body_hash,
                    collected_at,
                    metadata,
                )
            )
        return enriched


class BinanceAnnouncementCollector:
    def __init__(
        self,
        url: str,
        http: JsonHttpClient,
        *,
        known_ids_provider: Callable[[], Awaitable[set[str]]] | None = None,
        scanned_ids_provider: Callable[[], Awaitable[set[str]]] | None = None,
        recent_scanned_ids_provider: (
            Callable[[datetime], Awaitable[set[str]]] | None
        ) = None,
        recent_failed_ids_provider: (
            Callable[[datetime], Awaitable[set[str]]] | None
        ) = None,
        scan_record_writer: (
            Callable[[Announcement], Awaitable[object]] | None
        ) = None,
    ) -> None:
        if not _is_allowed_https("binance", url):
            raise ValueError("configured binance URL is outside allowlist")
        self.source = "binance"
        self._url = url
        self._http = http
        self._known_ids_provider = known_ids_provider
        self._scanned_ids_provider = scanned_ids_provider
        self._recent_scanned_ids_provider = recent_scanned_ids_provider
        self._recent_failed_ids_provider = recent_failed_ids_provider
        self._scan_record_writer = scan_record_writer

    async def collect(self, collected_at: datetime) -> list[Announcement]:
        known_ids = (
            await self._known_ids_provider()
            if self._known_ids_provider is not None
            else set()
        )
        scanned_ids = (
            await self._scanned_ids_provider()
            if self._scanned_ids_provider is not None
            else set()
        )
        recent_scanned_ids = (
            await self._recent_scanned_ids_provider(
                collected_at - BINANCE_BODY_RESCAN_AFTER
            )
            if self._recent_scanned_ids_provider is not None
            else scanned_ids
        )
        recent_failed_ids = (
            await self._recent_failed_ids_provider(
                collected_at - BINANCE_BODY_RESCAN_AFTER
            )
            if self._recent_failed_ids_provider is not None
            else set()
        )
        items_by_id: dict[str, Announcement] = {}
        never_scanned_candidates: dict[str, Announcement] = {}
        expired_scan_candidates: dict[str, Announcement] = {}
        deferred_failure_ids = set(recent_failed_ids)
        seen_entry_ids: set[str] = set()
        for page_number in range(1, BINANCE_MAX_PAGES + 1):
            payload = await self._http.get_json(
                self._url,
                params={
                    "type": 1,
                    "pageNo": page_number,
                    "pageSize": BINANCE_PAGE_SIZE,
                },
            )
            entries = _binance_entries(
                payload, allow_empty=page_number > 1
            )
            if not entries:
                break
            page_items = parse_binance_payload(
                payload,
                self._url,
                require_title_match=False,
                collected_at=collected_at,
            )
            entry_ids = {str(entry["code"]) for entry in entries}
            if page_number > 1 and entry_ids <= seen_entry_ids:
                break
            seen_entry_ids.update(entry_ids)
            for item in page_items:
                if item.stable_id in recent_failed_ids:
                    deferred_failure_ids.add(item.stable_id)
                    continue
                if (
                    matches_usd1(item.title)
                    or item.stable_id in known_ids
                ):
                    items_by_id.setdefault(item.stable_id, item)
                elif item.stable_id not in scanned_ids:
                    never_scanned_candidates.setdefault(item.stable_id, item)
                elif item.stable_id not in recent_scanned_ids:
                    expired_scan_candidates.setdefault(item.stable_id, item)
            if len(entries) < BINANCE_PAGE_SIZE:
                break
        candidate_sort_key = (
            lambda item: item.published_at or datetime.min.replace(tzinfo=UTC)
        )
        never_scanned = sorted(
            never_scanned_candidates.values(), key=candidate_sort_key
        )
        expired_scans = sorted(
            expired_scan_candidates.values(),
            key=candidate_sort_key,
            reverse=True,
        )
        body_scan_candidates = never_scanned[:BINANCE_BODY_SCAN_LIMIT]
        remaining_capacity = BINANCE_BODY_SCAN_LIMIT - len(body_scan_candidates)
        if remaining_capacity:
            body_scan_candidates.extend(
                expired_scans[:remaining_capacity]
            )
        priority_items = list(items_by_id.values())
        fair_items_by_id: dict[str, Announcement] = {}
        for index in range(max(len(priority_items), len(body_scan_candidates))):
            if index < len(priority_items):
                item = priority_items[index]
                fair_items_by_id.setdefault(item.stable_id, item)
            if index < len(body_scan_candidates):
                item = body_scan_candidates[index]
                fair_items_by_id.setdefault(item.stable_id, item)
        items = list(fair_items_by_id.values())
        enriched: list[Announcement] = []
        failures: list[tuple[str, Exception]] = []
        parts = urlsplit(self._url)
        detail_url = (
            f"{parts.scheme}://{parts.netloc}"
            "/bapi/composite/v1/public/cms/article/detail/query"
        )
        detail_semaphore = asyncio.Semaphore(BINANCE_DETAIL_CONCURRENCY)

        async def fetch_detail(
            item: Announcement,
        ) -> tuple[
            Announcement,
            str | None,
            list[str] | None,
            Exception | None,
            bool,
        ]:
            attempted = False
            try:
                async with asyncio.timeout(BINANCE_DETAIL_TIMEOUT_SECONDS):
                    async with detail_semaphore:
                        attempted = True
                        detail_payload = await self._http.get_json(
                            detail_url,
                            params={"articleCode": item.stable_id},
                        )
                    if (
                        not isinstance(detail_payload, dict)
                        or detail_payload.get("code") != "000000"
                        or not isinstance(detail_payload.get("data"), dict)
                        or not isinstance(
                            detail_payload["data"].get("body"), str
                        )
                    ):
                        raise PageStructureError(
                            "invalid Binance announcement detail response"
                        )
                    detail_html = detail_payload["data"]["body"]
                    detail_soup = BeautifulSoup(detail_html, "html.parser")
                    body_text = normalize_text(detail_soup.get_text(" "))
                    if not body_text:
                        raise PageStructureError(
                            "Binance detail page has no readable text"
                        )
                    body_sections = _body_sections(detail_soup, body_text)
            except Exception as exc:
                return item, None, None, exc, attempted
            return item, body_text, body_sections, None, attempted

        detail_results = await asyncio.gather(
            *(fetch_detail(item) for item in items)
        )
        for (
            item,
            body_text,
            body_sections,
            detail_error,
            attempted,
        ) in detail_results:
            if detail_error is not None:
                exc = detail_error
                failures.append((item.stable_id, exc))
                if attempted and self._scan_record_writer is not None:
                    await self._scan_record_writer(
                        Announcement(
                            "binance_scan",
                            item.stable_id,
                            item.title,
                            item.url,
                            item.published_at,
                            content_hash(
                                item.title,
                                f"detail-error:{type(exc).__name__}",
                            ),
                            collected_at,
                            {
                                "content_version": "detail-error-v1",
                                "scan_error": type(exc).__name__,
                                "usd1_relevant": False,
                            },
                        )
                    )
                continue
            assert body_text is not None
            assert body_sections is not None
            relevant = matches_usd1(f"{item.title} {body_text}")
            result = Announcement(
                item.source if relevant else "binance_scan",
                item.stable_id,
                item.title,
                item.url,
                item.published_at,
                content_hash(item.title, body_text),
                collected_at,
                {
                    "body_text": body_text,
                    "body_sections": body_sections,
                    "content_version": "body-v1",
                    "usd1_relevant": relevant,
                },
            )
            if relevant:
                enriched.append(result)
            elif self._scan_record_writer is not None:
                await self._scan_record_writer(result)
        if failures or deferred_failure_ids:
            raise BinancePartialCollectionError(
                enriched, failures, deferred_failure_ids
            )
        return enriched
