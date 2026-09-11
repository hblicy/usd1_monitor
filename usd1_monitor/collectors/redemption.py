from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ElementTree
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

from usd1_monitor.collectors.announcements import (
    PageStructureError,
    _detail_body_content,
    content_hash,
    normalize_text,
)
from usd1_monitor.engine.redemption_rules import classify_redemption
from usd1_monitor.models import Announcement, Observation, RiskLevel


__all__ = [
    "BitGoStatusCollector",
    "MediaRedemptionRssCollector",
    "OfficialRedemptionPageCollector",
    "RedemptionDataError",
    "RedemptionStatusSnapshot",
    "extract_sections",
    "parse_media_rss",
]


class RedemptionDataError(ValueError):
    """Raised when a redemption source cannot be interpreted safely."""


class JsonHttpClient(Protocol):
    async def get_json(
        self, url: str, params: dict[str, object] | None = None
    ) -> object: ...


class TextHttpClient(Protocol):
    async def get_text(self, url: str) -> str: ...


@dataclass(frozen=True)
class RedemptionStatusSnapshot:
    level: RiskLevel
    summary: str
    confirmed_usd1: bool
    matched_text: str | None
    observation: Observation


_RELEVANT_COMPONENTS = {
    "stablecoins": "Stablecoins",
    "settlement": "Settlement",
    "api": "API",
    "wallets": "Wallets",
}
_REQUIRED_COMPONENTS = ("stablecoins", "settlement")
_COMPONENT_STATUSES = frozenset(
    {
        "operational",
        "degraded_performance",
        "partial_outage",
        "major_outage",
        "under_maintenance",
    }
)
_OPEN_INCIDENT_STATUSES = frozenset({"investigating", "identified", "monitoring"})
_CLOSED_INCIDENT_STATUSES = frozenset({"resolved"})
_INCIDENT_STATUSES = _OPEN_INCIDENT_STATUSES | _CLOSED_INCIDENT_STATUSES
_USD1 = re.compile(r"(?<![A-Za-z0-9])usd1(?![A-Za-z0-9])", re.IGNORECASE)
_UNITAS = re.compile(r"(?<![A-Za-z0-9])unitas(?![A-Za-z0-9])", re.IGNORECASE)
_MEDIA_ENTITY = re.compile(
    r"(?<![A-Za-z0-9])usd1(?![A-Za-z0-9])|"
    r"(?<![A-Za-z0-9])world\s+liberty(?:\s+financial)?(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_MEDIA_TOPIC = re.compile(
    r"(?<![A-Za-z0-9])redeem(?:s|ed|ing|able)?(?![A-Za-z0-9])|"
    r"(?<![A-Za-z0-9])redemptions?(?![A-Za-z0-9])|"
    r"(?<![A-Za-z0-9])bank(?:s|ing)?(?![A-Za-z0-9])|"
    r"(?<![A-Za-z0-9])settlement(?:s)?(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_ATOM_NAMESPACE = "http://www.w3.org/2005/Atom"


def _checked_at(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RedemptionDataError("checked_at must be timezone-aware")
    return value


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not normalize_text(value):
        raise RedemptionDataError(f"invalid {field}")
    return normalize_text(value)


def _parse_source_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise RedemptionDataError(f"invalid {field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RedemptionDataError(f"invalid {field}") from exc
    if parsed.tzinfo is None:
        raise RedemptionDataError(f"invalid {field}")
    return parsed.astimezone(UTC)


def _component_key(name: str) -> str | None:
    canonical = normalize_text(name).casefold()
    return canonical if canonical in _RELEVANT_COMPONENTS else None


@dataclass(frozen=True)
class _StatusComponent:
    component_id: str
    canonical_name: str
    relevant_key: str | None


@dataclass(frozen=True)
class _ComponentRegistry:
    by_id: dict[str, _StatusComponent]
    by_name: dict[str, tuple[_StatusComponent, ...]]


def _parse_components(
    raw_components: object,
) -> tuple[dict[str, str], _ComponentRegistry]:
    if not isinstance(raw_components, list):
        raise RedemptionDataError("status response components must be a list")
    components: dict[str, str] = {}
    by_id: dict[str, _StatusComponent] = {}
    by_name_lists: dict[str, list[_StatusComponent]] = {}
    for index, raw in enumerate(raw_components):
        if not isinstance(raw, Mapping):
            raise RedemptionDataError(f"component {index} must be a mapping")
        name = _required_string(raw.get("name"), f"component {index} name")
        status = _required_string(raw.get("status"), f"component {name} status")
        if status not in _COMPONENT_STATUSES:
            raise RedemptionDataError(
                f"unknown component status for {name}: {status}"
            )
        component_id = _required_string(
            raw.get("id"), f"component {name} id"
        )
        key = _component_key(name)
        if key is not None and key in components:
            raise RedemptionDataError(
                f"duplicate {_RELEVANT_COMPONENTS[key]} component"
            )
        if component_id in by_id:
            raise RedemptionDataError(f"duplicate component id: {component_id}")
        if key is not None:
            components[key] = status
        component = _StatusComponent(
            component_id=component_id,
            canonical_name=name.casefold(),
            relevant_key=key,
        )
        by_id[component_id] = component
        by_name_lists.setdefault(component.canonical_name, []).append(component)
    for key in _REQUIRED_COMPONENTS:
        if key not in components:
            raise RedemptionDataError(
                f"missing {_RELEVANT_COMPONENTS[key]} component"
            )
    return components, _ComponentRegistry(
        by_id=by_id,
        by_name={
            name: tuple(values) for name, values in by_name_lists.items()
        },
    )


@dataclass(frozen=True)
class _Incident:
    active: bool
    text: str
    affected_components: frozenset[str]


def _parse_incident(
    raw: object,
    index: int,
    component_registry: _ComponentRegistry,
    seen_incident_ids: set[str],
    seen_update_ids: set[str],
) -> _Incident:
    if not isinstance(raw, Mapping):
        raise RedemptionDataError(f"incident {index} must be a mapping")
    incident_id = _required_string(raw.get("id"), f"incident {index} id")
    if incident_id in seen_incident_ids:
        raise RedemptionDataError(f"duplicate incident id: {incident_id}")
    seen_incident_ids.add(incident_id)
    name = _required_string(raw.get("name"), f"incident {index} name")
    status = _required_string(raw.get("status"), f"incident {index} status")
    if status not in _INCIDENT_STATUSES:
        raise RedemptionDataError(f"incident {index} has unknown status: {status}")

    created_at = _parse_source_datetime(
        raw.get("created_at"), f"incident {index} created_at"
    )
    updated_at = _parse_source_datetime(
        raw.get("updated_at"), f"incident {index} updated_at"
    )
    if updated_at < created_at:
        raise RedemptionDataError(f"incident {index} has invalid time range")

    resolved_at = raw.get("resolved_at")
    if status in _OPEN_INCIDENT_STATUSES:
        if resolved_at is not None:
            raise RedemptionDataError(
                f"incident {index} is open but has resolved_at"
            )
        active = True
    else:
        if resolved_at is None:
            raise RedemptionDataError(
                f"incident {index} resolved_at is required"
            )
        resolved_time = _parse_source_datetime(
            resolved_at, f"incident {index} resolved_at"
        )
        if not created_at <= resolved_time <= updated_at:
            raise RedemptionDataError(
                f"incident {index} resolved_at is outside time range"
            )
        active = False

    for field in ("started_at", "monitoring_at"):
        value = raw.get(field)
        if value is None:
            continue
        parsed = _parse_source_datetime(value, f"incident {index} {field}")
        if parsed > updated_at:
            raise RedemptionDataError(
                f"incident {index} {field} is outside time range"
            )

    raw_updates = raw.get("incident_updates")
    if not isinstance(raw_updates, list) or not raw_updates:
        raise RedemptionDataError(
            f"incident {index} incident_updates must be a non-empty list"
        )
    parsed_updates: list[tuple[datetime, str, str]] = []
    update_times: set[datetime] = set()
    for update_index, raw_update in enumerate(raw_updates):
        if not isinstance(raw_update, Mapping):
            raise RedemptionDataError(
                f"incident {index} update {update_index} must be a mapping"
            )
        update_id = _required_string(
            raw_update.get("id"),
            f"incident {index} update {update_index} id",
        )
        if update_id in seen_update_ids:
            raise RedemptionDataError(
                f"duplicate incident update id: {update_id}"
            )
        seen_update_ids.add(update_id)
        parent_id = _required_string(
            raw_update.get("incident_id"),
            f"incident {index} update {update_index} incident_id",
        )
        if parent_id != incident_id:
            raise RedemptionDataError(
                f"incident {index} update {update_index} incident_id mismatch"
            )
        update_status = _required_string(
            raw_update.get("status"),
            f"incident {index} update {update_index} status",
        )
        if update_status not in _INCIDENT_STATUSES:
            raise RedemptionDataError(
                f"incident {index} update {update_index} has unknown status"
            )
        update_created_at = _parse_source_datetime(
            raw_update.get("created_at"),
            f"incident {index} update {update_index} created_at",
        )
        update_updated_at = _parse_source_datetime(
            raw_update.get("updated_at"),
            f"incident {index} update {update_index} updated_at",
        )
        raw_display_at = raw_update.get("display_at")
        display_at = (
            _parse_source_datetime(
                raw_display_at,
                f"incident {index} update {update_index} display_at",
            )
            if raw_display_at is not None
            else update_created_at
        )
        if update_updated_at < update_created_at:
            raise RedemptionDataError(
                f"incident {index} update {update_index} has invalid time range"
            )
        if not (
            created_at <= update_created_at <= updated_at
            and update_updated_at <= updated_at
            and display_at <= updated_at
        ):
            raise RedemptionDataError(
                f"incident {index} update {update_index} is outside incident time range"
            )
        if display_at in update_times:
            raise RedemptionDataError(
                f"duplicate incident update time: {display_at.isoformat()}"
            )
        update_times.add(display_at)
        body = raw_update.get("body")
        if not isinstance(body, str):
            raise RedemptionDataError(
                f"incident {index} update {update_index} body must be text"
            )
        parsed_updates.append(
            (display_at, update_status, normalize_text(body))
        )
    parsed_updates.sort(key=lambda item: item[0])
    if parsed_updates[-1][1] != status:
        raise RedemptionDataError(
            f"incident {index} status does not match latest update status"
        )
    bodies = [body for _, _, body in parsed_updates if body]

    raw_affected = raw.get("components")
    if not isinstance(raw_affected, list):
        raise RedemptionDataError(f"incident {index} components must be a list")
    affected: set[str] = set()
    affected_ids: set[str] = set()
    for component_index, raw_component in enumerate(raw_affected):
        if not isinstance(raw_component, Mapping):
            raise RedemptionDataError(
                f"incident {index} component {component_index} must be a mapping"
            )
        component_status = _required_string(
            raw_component.get("status"),
            f"incident {index} component {component_index} status",
        )
        if component_status not in _COMPONENT_STATUSES:
            raise RedemptionDataError(
                f"incident {index} component has unknown status"
            )
        has_id = "id" in raw_component
        has_name = "name" in raw_component
        if not has_id and not has_name:
            raise RedemptionDataError(
                f"incident {index} component {component_index} has no id or name"
            )

        by_id: _StatusComponent | None = None
        if has_id:
            component_id = _required_string(
                raw_component.get("id"),
                f"incident {index} component {component_index} id",
            )
            by_id = component_registry.by_id.get(component_id)
            if by_id is None:
                raise RedemptionDataError(
                    f"incident {index} component {component_index} has unknown id"
                )

        by_name: _StatusComponent | None = None
        if has_name:
            component_name = _required_string(
                raw_component.get("name"),
                f"incident {index} component {component_index} name",
            )
            candidates = component_registry.by_name.get(
                component_name.casefold(), ()
            )
            if len(candidates) != 1:
                raise RedemptionDataError(
                    f"incident {index} component {component_index} has "
                    "unknown or ambiguous name"
                )
            by_name = candidates[0]

        if by_id is not None and by_name is not None and by_id != by_name:
            raise RedemptionDataError(
                f"incident {index} component {component_index} id/name conflict"
            )
        component = by_id or by_name
        assert component is not None
        if component.component_id in affected_ids:
            raise RedemptionDataError(
                f"duplicate incident component: {component.component_id}"
            )
        affected_ids.add(component.component_id)
        if component.relevant_key is not None:
            affected.add(component.relevant_key)

    return _Incident(
        active=active,
        text=normalize_text(" ".join((name, *bodies))),
        affected_components=frozenset(affected),
    )


class BitGoStatusCollector:
    def __init__(self, http: JsonHttpClient, status_url: str) -> None:
        parts = urlsplit(status_url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.fragment
        ):
            raise ValueError("BitGo status URL must be credential-free HTTPS")
        self._http = http
        self._status_url = status_url

    async def collect(self, checked_at: datetime) -> RedemptionStatusSnapshot:
        checked_at = _checked_at(checked_at)
        payload = await self._http.get_json(self._status_url)
        if not isinstance(payload, Mapping):
            raise RedemptionDataError("status response must be a mapping")
        if "components" not in payload:
            raise RedemptionDataError("status response has no components")
        if "incidents" not in payload:
            raise RedemptionDataError("status response has no incidents")
        raw_incidents = payload["incidents"]
        if not isinstance(raw_incidents, list):
            raise RedemptionDataError("status response incidents must be a list")
        components, component_registry = _parse_components(payload["components"])
        seen_incident_ids: set[str] = set()
        seen_update_ids: set[str] = set()
        incidents = [
            _parse_incident(
                raw,
                index,
                component_registry,
                seen_incident_ids,
                seen_update_ids,
            )
            for index, raw in enumerate(raw_incidents)
        ]

        level = RiskLevel.GREEN
        summary = "未发现官方限制"
        confirmed_usd1 = False
        matched_text: str | None = None
        generic_relevant_problem = any(
            components[key] != "operational" for key in _REQUIRED_COMPONENTS
        )
        for incident in incidents:
            if not incident.active:
                continue
            if _USD1.search(incident.text):
                classification = classify_redemption(
                    incident.text, usd1_specific=True
                )
                if classification.level > level or (
                    classification.level == level
                    and classification.confirmed_usd1
                    and not confirmed_usd1
                ):
                    level = classification.level
                    summary = classification.summary
                    confirmed_usd1 = classification.confirmed_usd1
                    matched_text = classification.matched_text
            if incident.affected_components.intersection(_REQUIRED_COMPONENTS):
                generic_relevant_problem = True

        if generic_relevant_problem and level < RiskLevel.YELLOW:
            level = RiskLevel.YELLOW
            summary = "可能影响 USD1，尚未确认"
            confirmed_usd1 = False
            matched_text = None

        observation = Observation(
            metric="redemption.channel_status",
            source="bitgo_status",
            scope="global",
            value=float(level),
            unit="risk_level",
            observed_at=checked_at,
            collected_at=checked_at,
            metadata={
                "summary": summary,
                "confirmed_usd1": confirmed_usd1,
                "source_url": self._status_url,
                "max_age_seconds": 900,
            },
        )
        return RedemptionStatusSnapshot(
            level=level,
            summary=summary,
            confirmed_usd1=confirmed_usd1,
            matched_text=matched_text,
            observation=observation,
        )


def extract_sections(page: str) -> list[str]:
    if not isinstance(page, str):
        raise TypeError("page must be a string")
    try:
        _, sections = _detail_body_content(page)
    except PageStructureError as exc:
        raise RedemptionDataError(f"invalid official page: {exc}") from exc
    if not sections:
        raise RedemptionDataError("invalid official page: no readable sections")
    return sections


def _remove_dot_segments(path: str) -> str:
    remaining = path
    output = ""
    while remaining:
        if remaining.startswith("../"):
            remaining = remaining[3:]
        elif remaining.startswith("./"):
            remaining = remaining[2:]
        elif remaining.startswith("/./"):
            remaining = remaining[2:]
        elif remaining == "/.":
            remaining = "/"
        elif remaining.startswith("/../"):
            remaining = remaining[3:]
            output = output.rsplit("/", 1)[0]
        elif remaining == "/..":
            remaining = "/"
            output = output.rsplit("/", 1)[0]
        elif remaining in {".", ".."}:
            remaining = ""
        else:
            next_slash = remaining.find("/", 1 if remaining.startswith("/") else 0)
            if next_slash < 0:
                output += remaining
                remaining = ""
            else:
                output += remaining[:next_slash]
                remaining = remaining[next_slash:]
    return output


def _canonical_page_url(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("official page URL is invalid") from exc
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
    ):
        raise ValueError("official page URL must be credential-free HTTPS")
    path = _remove_dot_segments(parts.path or "/")
    if not path.startswith("/"):
        path = f"/{path}"
    path = re.sub(r"/+$", "", path) or "/"
    netloc = parts.hostname.casefold()
    return path, urlunsplit(("https", netloc, path, "", ""))


class OfficialRedemptionPageCollector:
    def __init__(self, source: str, url: str, http: TextHttpClient) -> None:
        if not isinstance(source, str) or not source.strip():
            raise ValueError("official page source must not be empty")
        self.source = source.strip()
        self._request_url = url
        self._stable_id, self._canonical_url = _canonical_page_url(url)
        self._http = http

    async def collect(self, checked_at: datetime) -> Announcement:
        checked_at = _checked_at(checked_at)
        page = await self._http.get_text(self._request_url)
        try:
            body_text, body_sections = _detail_body_content(page)
        except PageStructureError as exc:
            raise RedemptionDataError(f"invalid official page: {exc}") from exc
        if not body_sections:
            raise RedemptionDataError(
                "invalid official page: no readable sections"
            )
        title = body_sections[0]
        return Announcement(
            source=self.source,
            stable_id=self._stable_id,
            title=title,
            url=self._canonical_url,
            published_at=None,
            body_hash=content_hash(title, body_text),
            first_seen_at=checked_at,
            metadata={
                "body_text": body_text,
                "body_sections": body_sections,
                "content_version": "body-v1",
            },
        )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(
    element: ElementTree.Element,
    names: tuple[str, ...],
    namespace: str | None,
) -> str:
    expected = {
        f"{{{namespace}}}{name}" if namespace is not None else name
        for name in names
    }
    for child in element:
        if child.tag in expected:
            value = "".join(child.itertext())
            if normalized := normalize_text(value):
                return normalized
    return ""


def _entry_link(
    element: ElementTree.Element, namespace: str | None
) -> str:
    expected = (
        f"{{{namespace}}}link" if namespace is not None else "link"
    )
    if namespace is None:
        for child in element:
            if child.tag != expected:
                continue
            candidate = child.attrib.get("href") or "".join(child.itertext())
            if normalized := normalize_text(candidate):
                return normalized
        return ""

    alternates: set[tuple[int, str, str, str]] = set()
    for child in element:
        if child.tag != expected:
            continue
        rel = normalize_text(child.attrib.get("rel", "alternate")).casefold()
        if rel != "alternate":
            continue
        candidate = normalize_text(child.attrib.get("href", ""))
        canonical = _canonical_media_link(candidate)
        if canonical is not None:
            language = normalize_text(child.attrib.get("hreflang", "")).casefold()
            if not language:
                language_rank = 0
            elif language == "en" or language.startswith("en-"):
                language_rank = 1
            else:
                language_rank = 2
            media_type = normalize_text(child.attrib.get("type", "")).casefold()
            alternates.add((language_rank, language, media_type, canonical))
    return min(alternates)[3] if alternates else ""


def _canonical_media_link(link: str) -> str | None:
    try:
        parts = urlsplit(link)
        port = parts.port
    except ValueError:
        return None
    if (
        parts.scheme.casefold() != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port is not None
        or parts.netloc.casefold() != parts.hostname.casefold()
    ):
        return None
    hostname = parts.hostname.casefold()
    path = parts.path or "/"
    return urlunsplit(("https", hostname, path, parts.query, ""))


def _parse_publication_date(value: str) -> datetime:
    if not value:
        raise RedemptionDataError("RSS item has no publication date")
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError) as exc:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise RedemptionDataError("RSS item has invalid publication date") from exc
    if parsed.tzinfo is None:
        raise RedemptionDataError("RSS item has invalid publication date")
    return parsed.astimezone(UTC)


def _media_relevant(title: str, summary: str) -> bool:
    combined = f"{title} {summary}"
    if _UNITAS.search(combined):
        return False
    return any(
        _MEDIA_ENTITY.search(field) and _MEDIA_TOPIC.search(field)
        for field in (title, summary)
    )


def _feed_entries(
    root: ElementTree.Element,
) -> list[tuple[ElementTree.Element, str | None]]:
    if root.tag == "rss":
        version = root.attrib.get("version")
        if version != "2.0":
            raise RedemptionDataError("invalid RSS feed structure")
        channels = [child for child in root if child.tag == "channel"]
        if len(channels) != 1:
            raise RedemptionDataError("invalid RSS feed structure")
        channel = channels[0]
        entries = [child for child in channel if child.tag == "item"]
        direct_ids = {id(entry) for entry in entries}
        if any(
            _local_name(element.tag) == "item" and id(element) not in direct_ids
            for element in root.iter()
        ):
            raise RedemptionDataError("invalid RSS feed structure")
        return [(entry, None) for entry in entries]

    atom_feed_tag = f"{{{_ATOM_NAMESPACE}}}feed"
    atom_entry_tag = f"{{{_ATOM_NAMESPACE}}}entry"
    if root.tag == atom_feed_tag:
        entries = [child for child in root if child.tag == atom_entry_tag]
        direct_ids = {id(entry) for entry in entries}
        if any(
            _local_name(element.tag) == "entry" and id(element) not in direct_ids
            for element in root.iter()
        ):
            raise RedemptionDataError("invalid Atom feed structure")
        return [(entry, _ATOM_NAMESPACE) for entry in entries]

    raise RedemptionDataError("invalid RSS/Atom feed structure")


def parse_media_rss(xml: str, checked_at: datetime) -> list[Announcement]:
    if not isinstance(xml, str):
        raise TypeError("xml must be a string")
    checked_at = _checked_at(checked_at)
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise RedemptionDataError(f"invalid RSS XML: {exc}") from exc

    results: list[Announcement] = []
    seen_links: set[str] = set()
    for entry, namespace in _feed_entries(root):
        title = _child_text(entry, ("title",), namespace)
        summary = _child_text(
            entry, ("description", "summary", "content"), namespace
        )
        if not title or not _media_relevant(title, summary):
            continue
        link = _canonical_media_link(_entry_link(entry, namespace))
        if link is None or link in seen_links:
            continue
        if namespace is None:
            publication_value = _child_text(entry, ("pubDate",), None)
        else:
            publication_value = _child_text(entry, ("published",), namespace)
            if not publication_value:
                publication_value = _child_text(entry, ("updated",), namespace)
        published_at = _parse_publication_date(publication_value)
        seen_links.add(link)
        results.append(
            Announcement(
                source="media_redemption",
                stable_id=hashlib.sha256(link.encode("utf-8")).hexdigest(),
                title=title,
                url=link,
                published_at=published_at,
                body_hash=content_hash(title, summary),
                first_seen_at=checked_at,
                metadata={
                    "verified": False,
                    "lead_only": True,
                    "summary": summary,
                },
            )
        )
    return results


class MediaRedemptionRssCollector:
    def __init__(self, http: TextHttpClient, url: str) -> None:
        canonical_url = _canonical_media_link(url)
        if canonical_url is None:
            raise ValueError("media RSS URL must be credential-free HTTPS")
        self._http = http
        self._url = canonical_url

    async def collect(self, checked_at: datetime) -> list[Announcement]:
        xml = await self._http.get_text(self._url)
        return parse_media_rss(xml, checked_at)
