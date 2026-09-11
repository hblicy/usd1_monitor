from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from usd1_monitor.models import RiskLevel


FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 9, 11, 4, 0, tzinfo=UTC)
STATUS_URL = "https://status.bitgo.com/api/v2/summary.json"


class FakeHttp:
    def __init__(self, *, json_value: object = None, text_value: str = "") -> None:
        self.json_value = json_value
        self.text_value = text_value
        self.calls: list[tuple[str, str]] = []

    async def get_json(
        self, url: str, params: dict[str, object] | None = None
    ) -> object:
        self.calls.append(("json", url))
        return self.json_value

    async def get_text(self, url: str) -> str:
        self.calls.append(("text", url))
        return self.text_value


def fixture_json(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def normal_status() -> dict[str, object]:
    value = fixture_json("bitgo_status_normal.json")
    assert isinstance(value, dict)
    return value


def active_incident(
    name: str,
    body: str,
    *,
    component_name: str = "Stablecoins",
    component_id: str = "stablecoins",
) -> dict[str, object]:
    return {
        "id": "incident-1",
        "name": name,
        "status": "investigating",
        "created_at": "2026-09-11T03:00:00Z",
        "updated_at": "2026-09-11T03:10:00Z",
        "resolved_at": None,
        "incident_updates": [
            {
                "id": "incident-1-update-1",
                "incident_id": "incident-1",
                "status": "investigating",
                "body": body,
                "created_at": "2026-09-11T03:10:00Z",
                "updated_at": "2026-09-11T03:10:00Z",
                "display_at": "2026-09-11T03:10:00Z",
            }
        ],
        "components": [
            {
                "id": component_id,
                "name": component_name,
                "status": "degraded_performance",
            }
        ],
    }


def incident_update(
    update_id: str,
    incident_id: str,
    status: str,
    body: str,
    created_at: str,
) -> dict[str, object]:
    return {
        "id": update_id,
        "incident_id": incident_id,
        "status": status,
        "body": body,
        "created_at": created_at,
        "updated_at": created_at,
        "display_at": created_at,
    }


@pytest.mark.asyncio
async def test_status_collector_returns_strict_normal_observation() -> None:
    from usd1_monitor.collectors.redemption import BitGoStatusCollector

    http = FakeHttp(json_value=normal_status())
    snapshot = await BitGoStatusCollector(http, STATUS_URL).collect(NOW)

    assert snapshot.level is RiskLevel.GREEN
    assert snapshot.confirmed_usd1 is False
    assert snapshot.summary == "未发现官方限制"
    assert snapshot.observation.metric == "redemption.channel_status"
    assert snapshot.observation.source == "bitgo_status"
    assert snapshot.observation.scope == "global"
    assert snapshot.observation.value == float(RiskLevel.GREEN)
    assert snapshot.observation.unit == "risk_level"
    assert snapshot.observation.observed_at == NOW
    assert snapshot.observation.collected_at == NOW
    assert snapshot.observation.metadata == {
        "summary": "未发现官方限制",
        "confirmed_usd1": False,
        "source_url": STATUS_URL,
        "max_age_seconds": 900,
    }
    assert http.calls == [("json", STATUS_URL)]


@pytest.mark.asyncio
async def test_status_collector_marks_relevant_generic_incident_unconfirmed() -> None:
    from usd1_monitor.collectors.redemption import BitGoStatusCollector

    snapshot = await BitGoStatusCollector(
        FakeHttp(json_value=fixture_json("bitgo_status_incident.json")),
        STATUS_URL,
    ).collect(NOW)

    assert snapshot.level is RiskLevel.YELLOW
    assert snapshot.confirmed_usd1 is False
    assert snapshot.summary == "可能影响 USD1，尚未确认"
    assert snapshot.observation.value == float(RiskLevel.YELLOW)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["Stablecoins", "Settlement"])
async def test_status_collector_rejects_missing_required_component(
    missing: str,
) -> None:
    from usd1_monitor.collectors.redemption import (
        BitGoStatusCollector,
        RedemptionDataError,
    )

    payload = normal_status()
    payload["components"] = [
        item
        for item in payload["components"]  # type: ignore[union-attr]
        if item["name"] != missing
    ]

    with pytest.raises(RedemptionDataError, match=f"missing {missing}"):
        await BitGoStatusCollector(FakeHttp(json_value=payload), STATUS_URL).collect(
            NOW
        )


@pytest.mark.asyncio
async def test_status_collector_rejects_duplicate_required_component() -> None:
    from usd1_monitor.collectors.redemption import (
        BitGoStatusCollector,
        RedemptionDataError,
    )

    payload = normal_status()
    components = payload["components"]
    assert isinstance(components, list)
    components.append(copy.deepcopy(components[0]))

    with pytest.raises(RedemptionDataError, match="duplicate Stablecoins"):
        await BitGoStatusCollector(FakeHttp(json_value=payload), STATUS_URL).collect(
            NOW
        )


@pytest.mark.asyncio
async def test_status_collector_rejects_duplicate_unrelated_component_id() -> None:
    from usd1_monitor.collectors.redemption import (
        BitGoStatusCollector,
        RedemptionDataError,
    )

    payload = normal_status()
    components = payload["components"]
    assert isinstance(components, list)
    components.append(
        {"id": "trading", "name": "Trading Reports", "status": "operational"}
    )

    with pytest.raises(RedemptionDataError, match="duplicate component id"):
        await BitGoStatusCollector(FakeHttp(json_value=payload), STATUS_URL).collect(
            NOW
        )


@pytest.mark.asyncio
async def test_status_collector_rejects_unknown_relevant_status() -> None:
    from usd1_monitor.collectors.redemption import (
        BitGoStatusCollector,
        RedemptionDataError,
    )

    payload = normal_status()
    components = payload["components"]
    assert isinstance(components, list)
    components[0]["status"] = "mostly_operational"

    with pytest.raises(RedemptionDataError, match="unknown component status"):
        await BitGoStatusCollector(FakeHttp(json_value=payload), STATUS_URL).collect(
            NOW
        )


@pytest.mark.asyncio
async def test_status_collector_rejects_unknown_unrelated_component_status() -> None:
    from usd1_monitor.collectors.redemption import (
        BitGoStatusCollector,
        RedemptionDataError,
    )

    payload = normal_status()
    components = payload["components"]
    assert isinstance(components, list)
    components[-1]["status"] = "mostly_operational"

    with pytest.raises(RedemptionDataError, match="unknown component status"):
        await BitGoStatusCollector(FakeHttp(json_value=payload), STATUS_URL).collect(
            NOW
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (None, "mapping"),
        ({}, "components"),
        ({"components": {}, "incidents": []}, "components"),
        ({"components": [], "incidents": {}}, "incidents"),
    ],
)
async def test_status_collector_rejects_empty_or_malformed_response(
    payload: object, message: str
) -> None:
    from usd1_monitor.collectors.redemption import (
        BitGoStatusCollector,
        RedemptionDataError,
    )

    with pytest.raises(RedemptionDataError, match=message):
        await BitGoStatusCollector(FakeHttp(json_value=payload), STATUS_URL).collect(
            NOW
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        {"status": "unknown"},
        {"status": "resolved", "resolved_at": None},
        {
            "status": "investigating",
            "resolved_at": "2026-09-11T03:00:00Z",
        },
        {"incident_updates": {}},
        {"components": {}},
    ],
)
async def test_status_collector_rejects_malformed_incident(
    mutation: dict[str, object],
) -> None:
    from usd1_monitor.collectors.redemption import (
        BitGoStatusCollector,
        RedemptionDataError,
    )

    payload = normal_status()
    incident = active_incident("Settlement delay", "Settlement is delayed.")
    incident.update(mutation)
    payload["incidents"] = [incident]

    with pytest.raises(RedemptionDataError, match="incident"):
        await BitGoStatusCollector(FakeHttp(json_value=payload), STATUS_URL).collect(
            NOW
        )


@pytest.mark.asyncio
async def test_status_collector_rejects_unknown_incident_component_status() -> None:
    from usd1_monitor.collectors.redemption import (
        BitGoStatusCollector,
        RedemptionDataError,
    )

    payload = normal_status()
    incident = active_incident(
        "Trading outage",
        "Trading is unavailable.",
        component_name="Trading",
        component_id="trading",
    )
    incident_components = incident["components"]
    assert isinstance(incident_components, list)
    incident_components[0]["status"] = "mostly_operational"
    payload["incidents"] = [incident]

    with pytest.raises(RedemptionDataError, match="unknown status"):
        await BitGoStatusCollector(FakeHttp(json_value=payload), STATUS_URL).collect(
            NOW
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "component_ref",
    [
        {"id": "wallets", "name": "Settlement", "status": "major_outage"},
        {"id": "missing", "name": "Wallets", "status": "major_outage"},
        {"name": "Missing component", "status": "major_outage"},
        {"status": "major_outage"},
    ],
)
async def test_status_collector_rejects_conflicting_or_unknown_component_ref(
    component_ref: dict[str, object],
) -> None:
    from usd1_monitor.collectors.redemption import (
        BitGoStatusCollector,
        RedemptionDataError,
    )

    payload = normal_status()
    incident = active_incident("Service outage", "Service is unavailable.")
    incident["components"] = [component_ref]
    payload["incidents"] = [incident]

    with pytest.raises(RedemptionDataError, match="incident .* component"):
        await BitGoStatusCollector(FakeHttp(json_value=payload), STATUS_URL).collect(
            NOW
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("component_ref", "expected"),
    [
        ({"id": "settlement", "status": "major_outage"}, RiskLevel.YELLOW),
        ({"name": "Settlement", "status": "major_outage"}, RiskLevel.YELLOW),
        ({"id": "wallets", "status": "major_outage"}, RiskLevel.GREEN),
    ],
)
async def test_status_collector_resolves_id_only_or_name_only_component_ref(
    component_ref: dict[str, object], expected: RiskLevel
) -> None:
    from usd1_monitor.collectors.redemption import BitGoStatusCollector

    payload = normal_status()
    incident = active_incident("Service outage", "Service is unavailable.")
    incident["components"] = [component_ref]
    payload["incidents"] = [incident]

    snapshot = await BitGoStatusCollector(
        FakeHttp(json_value=payload), STATUS_URL
    ).collect(NOW)

    assert snapshot.level is expected


@pytest.mark.asyncio
async def test_status_collector_rejects_duplicate_incident_component_ref() -> None:
    from usd1_monitor.collectors.redemption import (
        BitGoStatusCollector,
        RedemptionDataError,
    )

    payload = normal_status()
    incident = active_incident("Settlement outage", "Service is unavailable.")
    incident["components"] = [
        {"id": "settlement", "status": "major_outage"},
        {"name": "Settlement", "status": "major_outage"},
    ]
    payload["incidents"] = [incident]

    with pytest.raises(RedemptionDataError, match="duplicate incident component"):
        await BitGoStatusCollector(FakeHttp(json_value=payload), STATUS_URL).collect(
            NOW
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"id": ""}, "incident 0 id"),
        ({"created_at": "2026-09-11T03:00:00"}, "created_at"),
        ({"updated_at": "2026-09-11T02:59:00Z"}, "time range"),
    ],
)
async def test_status_collector_rejects_invalid_incident_identity_or_time(
    mutation: dict[str, object], message: str
) -> None:
    from usd1_monitor.collectors.redemption import (
        BitGoStatusCollector,
        RedemptionDataError,
    )

    payload = normal_status()
    incident = active_incident("Wallet outage", "Wallets are unavailable.")
    incident.update(mutation)
    payload["incidents"] = [incident]

    with pytest.raises(RedemptionDataError, match=message):
        await BitGoStatusCollector(FakeHttp(json_value=payload), STATUS_URL).collect(
            NOW
        )


@pytest.mark.asyncio
async def test_status_collector_rejects_duplicate_incident_ids() -> None:
    from usd1_monitor.collectors.redemption import (
        BitGoStatusCollector,
        RedemptionDataError,
    )

    payload = normal_status()
    first = active_incident("Wallet outage", "Wallets are unavailable.")
    second = copy.deepcopy(first)
    second["incident_updates"][0]["id"] = "incident-1-update-2"  # type: ignore[index]
    payload["incidents"] = [first, second]

    with pytest.raises(RedemptionDataError, match="duplicate incident id"):
        await BitGoStatusCollector(FakeHttp(json_value=payload), STATUS_URL).collect(
            NOW
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("updates", "message"),
    [
        (
            [
                incident_update(
                    "same", "incident-1", "identified", "First", "2026-09-11T03:05:00Z"
                ),
                incident_update(
                    "same", "incident-1", "investigating", "Second", "2026-09-11T03:10:00Z"
                ),
            ],
            "duplicate incident update id",
        ),
        (
            [
                incident_update(
                    "update-1", "another-incident", "investigating", "Wrong parent", "2026-09-11T03:10:00Z"
                )
            ],
            "incident_id",
        ),
        (
            [
                incident_update(
                    "update-1", "incident-1", "identified", "First", "2026-09-11T03:10:00Z"
                ),
                incident_update(
                    "update-2", "incident-1", "investigating", "Second", "2026-09-11T03:10:00Z"
                ),
            ],
            "duplicate incident update time",
        ),
        (
            [
                incident_update(
                    "update-1", "incident-1", "investigating", "Bad time", "2026-09-11T03:10:00"
                )
            ],
            "created_at",
        ),
        (
            [
                {
                    **incident_update(
                        "update-1",
                        "incident-1",
                        "investigating",
                        "Late update",
                        "2026-09-11T03:10:00Z",
                    ),
                    "updated_at": "2026-09-11T03:11:00Z",
                }
            ],
            "outside incident time range",
        ),
    ],
)
async def test_status_collector_rejects_invalid_incident_updates(
    updates: list[dict[str, object]], message: str
) -> None:
    from usd1_monitor.collectors.redemption import (
        BitGoStatusCollector,
        RedemptionDataError,
    )

    payload = normal_status()
    incident = active_incident("Wallet outage", "Wallets are unavailable.")
    incident["incident_updates"] = updates
    payload["incidents"] = [incident]

    with pytest.raises(RedemptionDataError, match=message):
        await BitGoStatusCollector(FakeHttp(json_value=payload), STATUS_URL).collect(
            NOW
        )


@pytest.mark.asyncio
async def test_status_collector_requires_top_status_to_match_latest_update() -> None:
    from usd1_monitor.collectors.redemption import (
        BitGoStatusCollector,
        RedemptionDataError,
    )

    payload = normal_status()
    incident = active_incident("Wallet outage", "Wallets are unavailable.")
    incident["status"] = "monitoring"
    payload["incidents"] = [incident]

    with pytest.raises(RedemptionDataError, match="latest update status"):
        await BitGoStatusCollector(FakeHttp(json_value=payload), STATUS_URL).collect(
            NOW
        )


@pytest.mark.asyncio
async def test_status_collector_sorts_updates_before_classification() -> None:
    from usd1_monitor.collectors.redemption import BitGoStatusCollector

    payload = normal_status()
    incident = active_incident(
        "USD1 redemption incident",
        "unused",
        component_name="Wallets",
        component_id="wallets",
    )
    incident["status"] = "monitoring"
    incident["updated_at"] = "2026-09-11T03:20:00Z"
    incident["incident_updates"] = [
        incident_update(
            "update-new",
            "incident-1",
            "monitoring",
            "USD1 redemption is now operational.",
            "2026-09-11T03:20:00Z",
        ),
        incident_update(
            "update-old",
            "incident-1",
            "investigating",
            "USD1 redemption is suspended.",
            "2026-09-11T03:10:00Z",
        ),
    ]
    payload["incidents"] = [incident]

    snapshot = await BitGoStatusCollector(
        FakeHttp(json_value=payload), STATUS_URL
    ).collect(NOW)

    assert snapshot.level is RiskLevel.GREEN
    assert snapshot.summary == "未发现官方限制"


@pytest.mark.asyncio
async def test_status_collector_orders_backfilled_updates_by_display_time() -> None:
    from usd1_monitor.collectors.redemption import BitGoStatusCollector

    payload = normal_status()
    incident = active_incident(
        "USD1 redemption incident",
        "unused",
        component_name="Wallets",
        component_id="wallets",
    )
    incident["status"] = "monitoring"
    incident["updated_at"] = "2026-09-11T03:20:00Z"
    backfilled_stop = incident_update(
        "update-backfilled",
        "incident-1",
        "investigating",
        "USD1 redemption is suspended.",
        "2026-09-11T03:20:00Z",
    )
    backfilled_stop["display_at"] = "2026-09-11T03:10:00Z"
    recovered = incident_update(
        "update-recovered",
        "incident-1",
        "monitoring",
        "USD1 redemption is now operational.",
        "2026-09-11T03:15:00Z",
    )
    incident["incident_updates"] = [recovered, backfilled_stop]
    payload["incidents"] = [incident]

    snapshot = await BitGoStatusCollector(
        FakeHttp(json_value=payload), STATUS_URL
    ).collect(NOW)

    assert snapshot.level is RiskLevel.GREEN


@pytest.mark.asyncio
async def test_status_collector_falls_back_to_created_at_without_display_at() -> None:
    from usd1_monitor.collectors.redemption import BitGoStatusCollector

    payload = normal_status()
    incident = active_incident(
        "Wallet outage",
        "Wallets are unavailable.",
        component_name="Wallets",
        component_id="wallets",
    )
    del incident["incident_updates"][0]["display_at"]  # type: ignore[index]
    payload["incidents"] = [incident]

    snapshot = await BitGoStatusCollector(
        FakeHttp(json_value=payload), STATUS_URL
    ).collect(NOW)

    assert snapshot.level is RiskLevel.GREEN


@pytest.mark.asyncio
async def test_resolved_incident_with_old_suspension_cannot_be_red() -> None:
    from usd1_monitor.collectors.redemption import BitGoStatusCollector

    payload = normal_status()
    incident = active_incident("USD1 redemption incident", "unused")
    incident["status"] = "resolved"
    incident["updated_at"] = "2026-09-11T03:20:00Z"
    incident["resolved_at"] = "2026-09-11T03:20:00Z"
    incident["incident_updates"] = [
        incident_update(
            "update-resolved",
            "incident-1",
            "resolved",
            "USD1 redemption is now operational.",
            "2026-09-11T03:20:00Z",
        ),
        incident_update(
            "update-old",
            "incident-1",
            "investigating",
            "USD1 redemption is suspended.",
            "2026-09-11T03:10:00Z",
        ),
    ]
    payload["incidents"] = [incident]

    snapshot = await BitGoStatusCollector(
        FakeHttp(json_value=payload), STATUS_URL
    ).collect(NOW)

    assert snapshot.level is RiskLevel.GREEN


@pytest.mark.asyncio
async def test_resolved_incident_requires_resolved_at() -> None:
    from usd1_monitor.collectors.redemption import (
        BitGoStatusCollector,
        RedemptionDataError,
    )

    payload = normal_status()
    incident = active_incident("Resolved wallet outage", "Recovered.")
    incident["status"] = "resolved"
    incident["resolved_at"] = None
    incident["incident_updates"][0]["status"] = "resolved"  # type: ignore[index]
    payload["incidents"] = [incident]

    with pytest.raises(RedemptionDataError, match="resolved_at"):
        await BitGoStatusCollector(FakeHttp(json_value=payload), STATUS_URL).collect(
            NOW
        )


@pytest.mark.asyncio
async def test_status_collector_rejects_postmortem_status() -> None:
    from usd1_monitor.collectors.redemption import (
        BitGoStatusCollector,
        RedemptionDataError,
    )

    payload = normal_status()
    incident = active_incident("Old incident", "Recovered.")
    incident["status"] = "postmortem"
    incident["resolved_at"] = "2026-09-11T03:10:00Z"
    incident["incident_updates"][0]["status"] = "postmortem"  # type: ignore[index]
    payload["incidents"] = [incident]

    with pytest.raises(RedemptionDataError, match="unknown status"):
        await BitGoStatusCollector(FakeHttp(json_value=payload), STATUS_URL).collect(
            NOW
        )


@pytest.mark.asyncio
async def test_status_collector_ignores_resolved_incident() -> None:
    from usd1_monitor.collectors.redemption import BitGoStatusCollector

    snapshot = await BitGoStatusCollector(
        FakeHttp(json_value=normal_status()), STATUS_URL
    ).collect(NOW)

    assert snapshot.level is RiskLevel.GREEN


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("USD1 redemption is suspended.", RiskLevel.RED),
        ("USD1 redemption is currently delayed.", RiskLevel.YELLOW),
    ],
)
async def test_status_collector_classifies_explicit_usd1_incident(
    text: str, expected: RiskLevel
) -> None:
    from usd1_monitor.collectors.redemption import BitGoStatusCollector

    payload = normal_status()
    payload["incidents"] = [active_incident(text, text)]

    snapshot = await BitGoStatusCollector(
        FakeHttp(json_value=payload), STATUS_URL
    ).collect(NOW)

    assert snapshot.level is expected
    assert snapshot.confirmed_usd1 is True
    assert snapshot.observation.metadata["confirmed_usd1"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("component_name", ["Wallets", "API"])
async def test_wallet_or_api_failure_alone_does_not_change_redemption_level(
    component_name: str,
) -> None:
    from usd1_monitor.collectors.redemption import BitGoStatusCollector

    payload = normal_status()
    components = payload["components"]
    assert isinstance(components, list)
    component = next(item for item in components if item["name"] == component_name)
    component["status"] = "major_outage"
    payload["incidents"] = [
        active_incident(
            f"{component_name} outage",
            f"{component_name} is unavailable.",
            component_name=component_name,
            component_id=str(component["id"]),
        )
    ]

    snapshot = await BitGoStatusCollector(
        FakeHttp(json_value=payload), STATUS_URL
    ).collect(NOW)

    assert snapshot.level is RiskLevel.GREEN
    assert snapshot.summary == "未发现官方限制"


@pytest.mark.asyncio
async def test_wallet_incident_explicitly_naming_usd1_is_classified() -> None:
    from usd1_monitor.collectors.redemption import BitGoStatusCollector

    payload = normal_status()
    payload["incidents"] = [
        active_incident(
            "Wallet outage",
            "USD1 redemption is unavailable.",
            component_name="Wallets",
            component_id="wallets",
        )
    ]

    snapshot = await BitGoStatusCollector(
        FakeHttp(json_value=payload), STATUS_URL
    ).collect(NOW)

    assert snapshot.level is RiskLevel.RED
    assert snapshot.confirmed_usd1 is True


@pytest.mark.asyncio
async def test_similar_component_name_cannot_leak_into_relevant_components() -> None:
    from usd1_monitor.collectors.redemption import BitGoStatusCollector

    payload = normal_status()
    components = payload["components"]
    assert isinstance(components, list)
    components.append(
        {
            "id": "legacy-stablecoins",
            "name": "Stablecoins Legacy",
            "status": "major_outage",
        }
    )
    payload["incidents"] = [
        active_incident(
            "Legacy service outage",
            "Legacy service is unavailable.",
            component_name="Stablecoins Legacy",
            component_id="legacy-stablecoins",
        )
    ]

    snapshot = await BitGoStatusCollector(
        FakeHttp(json_value=payload), STATUS_URL
    ).collect(NOW)

    assert snapshot.level is RiskLevel.GREEN


def test_extract_sections_reads_multiple_main_sections_without_layout_noise() -> None:
    from usd1_monitor.collectors.redemption import extract_sections

    html = """
    <html><body><nav>Navigation</nav><main>
      <h1>USD1 redemption</h1>
      <p>Requests are available.</p>
      <section><p>Bank settlement information.</p></section>
      <footer>Footer</footer>
    </main></body></html>
    """

    assert extract_sections(html) == [
        "USD1 redemption",
        "Requests are available.",
        "Bank settlement information.",
    ]


@pytest.mark.parametrize(
    "html",
    [
        "<html><body><div>Shell only</div></body></html>",
        "<html><body><main>   </main></body></html>",
        "",
    ],
)
def test_extract_sections_rejects_empty_or_unexpected_page(html: str) -> None:
    from usd1_monitor.collectors.redemption import (
        RedemptionDataError,
        extract_sections,
    )

    with pytest.raises(RedemptionDataError, match="official page"):
        extract_sections(html)


@pytest.mark.asyncio
async def test_official_page_collector_uses_normalized_url_path_as_stable_id() -> None:
    from usd1_monitor.collectors.redemption import OfficialRedemptionPageCollector

    http = FakeHttp(
        text_value="<main><h1>USD1</h1><p>Redemption information.</p></main>"
    )
    item = await OfficialRedemptionPageCollector(
        "bitgo_usd1", "https://www.bitgo.com/usd1/?source=nav#terms", http
    ).collect(NOW)

    assert item.source == "bitgo_usd1"
    assert item.stable_id == "/usd1"
    assert item.url == "https://www.bitgo.com/usd1"
    assert item.first_seen_at == NOW
    assert item.metadata["body_sections"] == [
        "USD1",
        "Redemption information.",
    ]
    assert item.metadata["body_text"] == "USD1 Redemption information."


@pytest.mark.asyncio
async def test_official_page_stable_ids_distinguish_paths_and_normalize_root() -> None:
    from usd1_monitor.collectors.redemption import OfficialRedemptionPageCollector

    http = FakeHttp(text_value="<main><p>Official USD1 information.</p></main>")
    root = await OfficialRedemptionPageCollector(
        "bitgo_page", "https://www.bitgo.com/?v=1", http
    ).collect(NOW)
    terms = await OfficialRedemptionPageCollector(
        "bitgo_page", "https://www.bitgo.com/usd1-terms///?v=2", http
    ).collect(NOW)

    assert root.stable_id == "/"
    assert terms.stable_id == "/usd1-terms"
    assert root.stable_id != terms.stable_id


@pytest.mark.asyncio
async def test_official_page_stable_id_removes_rfc3986_dot_segments() -> None:
    from usd1_monitor.collectors.redemption import OfficialRedemptionPageCollector

    http = FakeHttp(text_value="<main><p>Official USD1 information.</p></main>")
    dotted = await OfficialRedemptionPageCollector(
        "bitgo_page", "https://www.bitgo.com/a/../usd1/?v=1#top", http
    ).collect(NOW)
    canonical = await OfficialRedemptionPageCollector(
        "bitgo_page", "https://www.bitgo.com/usd1", http
    ).collect(NOW)

    assert dotted.stable_id == "/usd1"
    assert dotted.stable_id == canonical.stable_id
    assert dotted.url == "https://www.bitgo.com/usd1"


@pytest.mark.asyncio
async def test_official_page_dot_removal_preserves_empty_path_segments() -> None:
    from usd1_monitor.collectors.redemption import OfficialRedemptionPageCollector

    http = FakeHttp(text_value="<main><p>Official USD1 information.</p></main>")
    item = await OfficialRedemptionPageCollector(
        "bitgo_page", "https://www.bitgo.com/a//b/../usd1/", http
    ).collect(NOW)

    assert item.stable_id == "/a//usd1"
    assert item.url == "https://www.bitgo.com/a//usd1"


def test_media_rss_excludes_unitas_and_marks_leads_unverified() -> None:
    from usd1_monitor.collectors.redemption import parse_media_rss

    items = parse_media_rss(fixture_text("redemption_media.xml"), NOW)

    assert [item.source for item in items] == ["media_redemption"]
    assert items[0].title == "USD1 bank settlement update"
    assert items[0].published_at == datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    assert items[0].first_seen_at == NOW
    assert items[0].metadata == {
        "verified": False,
        "lead_only": True,
        "summary": "World Liberty settlement coverage.",
    }


@pytest.mark.asyncio
async def test_media_rss_collector_fetches_and_parses_configured_feed() -> None:
    from usd1_monitor.collectors.redemption import MediaRedemptionRssCollector

    url = "https://news.example.com/redemption.xml"
    http = FakeHttp(text_value=fixture_text("redemption_media.xml"))

    items = await MediaRedemptionRssCollector(http, url).collect(NOW)

    assert len(items) == 1
    assert items[0].source == "media_redemption"
    assert http.calls == [("text", url)]


@pytest.mark.parametrize(
    ("xml", "message"),
    [
        ("<rss>", "XML"),
        (
            "<rss version='2.0'><channel><item><title>USD1 redemption</title>"
            "<link>https://news.example/x</link>"
            "<description>USD1 redemption report</description>"
            "</item></channel></rss>",
            "publication date",
        ),
        (
            "<rss version='2.0'><channel><item><title>USD1 redemption</title>"
            "<link>https://news.example/x</link>"
            "<description>USD1 redemption report</description>"
            "<pubDate>not-a-date</pubDate></item></channel></rss>",
            "publication date",
        ),
    ],
)
def test_media_rss_rejects_invalid_xml_or_date(xml: str, message: str) -> None:
    from usd1_monitor.collectors.redemption import (
        RedemptionDataError,
        parse_media_rss,
    )

    with pytest.raises(RedemptionDataError, match=message):
        parse_media_rss(xml, NOW)


@pytest.mark.parametrize(
    "link",
    [
        "http://news.example/usd1",
        "https://user:secret@news.example/usd1",
        "https://news.example:443/usd1",
        "https://news.example:8443/usd1",
    ],
)
def test_media_rss_rejects_non_https_or_credentialed_link(link: str) -> None:
    from usd1_monitor.collectors.redemption import parse_media_rss

    xml = f"""
    <rss version="2.0"><channel><item><title>USD1 redemption report</title>
    <link>{link}</link><description>USD1 redemption report.</description>
    <pubDate>Thu, 10 Sep 2026 12:00:00 GMT</pubDate>
    </item></channel></rss>
    """

    assert parse_media_rss(xml, NOW) == []


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@news.example/redemption.xml",
        "https://news.example:443/redemption.xml",
        "https://news.example:8443/redemption.xml",
    ],
)
def test_media_rss_collector_rejects_credentials_and_explicit_ports(
    url: str,
) -> None:
    from usd1_monitor.collectors.redemption import MediaRedemptionRssCollector

    with pytest.raises(ValueError, match="credential-free HTTPS"):
        MediaRedemptionRssCollector(FakeHttp(), url)


def test_media_rss_canonicalizes_fragment_and_deduplicates_stably() -> None:
    from usd1_monitor.collectors.redemption import parse_media_rss

    xml = """
    <rss version="2.0"><channel>
      <item><title>USD1 redemption report</title>
        <link>https://NEWS.example/report?id=1#top</link>
        <description>USD1 redemption report.</description>
        <pubDate>Thu, 10 Sep 2026 12:00:00 GMT</pubDate></item>
      <item><title>USD1 redemption report duplicate</title>
        <link>https://news.example/report?id=1#comments</link>
        <description>USD1 redemption report.</description>
        <pubDate>Thu, 10 Sep 2026 13:00:00 GMT</pubDate></item>
    </channel></rss>
    """

    items = parse_media_rss(xml, NOW)

    canonical = "https://news.example/report?id=1"
    assert len(items) == 1
    assert items[0].url == canonical
    assert items[0].stable_id == hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()


@pytest.mark.parametrize(
    ("title", "summary", "expected"),
    [
        ("World Liberty Financial bank settlement", "Report", 1),
        ("USD1 redemption", "Report", 1),
        ("USD10 redemption", "Report", 0),
        ("The unitasking USD1 redemption report", "Report", 1),
        ("Unitas USD1 redemption report", "Report", 0),
        ("World Liberty Financial：bank settlement", "Report", 1),
        ("USD1 update", "Bank settlement may be delayed", 0),
    ],
)
def test_media_rss_uses_entity_topic_and_unitas_word_boundaries(
    title: str, summary: str, expected: int
) -> None:
    from usd1_monitor.collectors.redemption import parse_media_rss

    xml = f"""
    <rss version="2.0"><channel><item><title>{title}</title>
    <link>https://news.example/article</link><description>{summary}</description>
    <pubDate>Thu, 10 Sep 2026 12:00:00 GMT</pubDate>
    </item></channel></rss>
    """

    assert len(parse_media_rss(xml, NOW)) == expected


@pytest.mark.parametrize(
    "xml",
    [
        "<html><body><item /></body></html>",
        "<root />",
        "<rss><channel /></rss>",
        "<rss version='1.0'><channel /></rss>",
        "<rss version='2.0' />",
        "<rss version='2.0'><channel><group><item /></group></channel></rss>",
        "<feed><entry /></feed>",
        (
            "<feed xmlns='http://www.w3.org/2005/Atom'>"
            "<group><entry /></group></feed>"
        ),
    ],
)
def test_media_rss_rejects_non_feed_or_nested_fake_entries(xml: str) -> None:
    from usd1_monitor.collectors.redemption import (
        RedemptionDataError,
        parse_media_rss,
    )

    with pytest.raises(RedemptionDataError, match="feed structure"):
        parse_media_rss(xml, NOW)


def test_media_rss_accepts_empty_well_formed_rss_and_atom_feeds() -> None:
    from usd1_monitor.collectors.redemption import parse_media_rss

    assert parse_media_rss("<rss version='2.0'><channel /></rss>", NOW) == []
    assert (
        parse_media_rss(
            "<feed xmlns='http://www.w3.org/2005/Atom'></feed>", NOW
        )
        == []
    )


def test_media_rss_accepts_direct_atom_entry_in_atom_namespace() -> None:
    from usd1_monitor.collectors.redemption import parse_media_rss

    xml = """
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>USD1 redemption report</title>
        <link href="https://news.example/atom-report#section" />
        <summary>USD1 redemption report.</summary>
        <published>2026-09-10T12:00:00Z</published>
      </entry>
    </feed>
    """

    items = parse_media_rss(xml, NOW)

    assert len(items) == 1
    assert items[0].url == "https://news.example/atom-report"
    assert items[0].published_at == datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def test_atom_prefers_alternate_link_when_self_link_comes_first() -> None:
    from usd1_monitor.collectors.redemption import parse_media_rss

    xml = """
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>USD1 redemption report</title>
        <link rel="self" href="https://news.example/feed-entry" />
        <link rel="alternate" href="https://news.example/article" />
        <summary>USD1 redemption report.</summary>
        <published>2026-09-10T12:00:00Z</published>
      </entry>
    </feed>
    """

    items = parse_media_rss(xml, NOW)

    assert len(items) == 1
    assert items[0].url == "https://news.example/article"


def test_atom_prefers_published_when_updated_comes_first() -> None:
    from usd1_monitor.collectors.redemption import parse_media_rss

    xml = """
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>USD1 redemption report</title>
        <link href="https://news.example/article" />
        <summary>USD1 redemption report.</summary>
        <updated>2026-09-11T12:00:00Z</updated>
        <published>2026-09-10T12:00:00Z</published>
      </entry>
    </feed>
    """

    items = parse_media_rss(xml, NOW)

    assert items[0].published_at == datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def test_atom_selects_multiple_alternate_links_deterministically() -> None:
    from usd1_monitor.collectors.redemption import parse_media_rss

    first_order = """
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>USD1 redemption report</title>
        <link href="https://news.example/z-no-language" type="text/html" />
        <link rel="alternate" hreflang="en" href="https://news.example/a-en" />
        <link rel="alternate" href="https://news.example/b-no-language" />
        <summary>USD1 redemption report.</summary>
        <published>2026-09-10T12:00:00Z</published>
      </entry>
    </feed>
    """
    reversed_order = """
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>USD1 redemption report</title>
        <link rel="alternate" href="https://news.example/b-no-language" />
        <link rel="alternate" hreflang="en" href="https://news.example/a-en" />
        <link href="https://news.example/z-no-language" type="text/html" />
        <summary>USD1 redemption report.</summary>
        <published>2026-09-10T12:00:00Z</published>
      </entry>
    </feed>
    """

    first = parse_media_rss(first_order, NOW)
    second = parse_media_rss(reversed_order, NOW)

    assert first[0].url == "https://news.example/b-no-language"
    assert second[0].url == first[0].url
