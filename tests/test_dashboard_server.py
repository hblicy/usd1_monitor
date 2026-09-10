import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from usd1_monitor.config import AppConfig
from usd1_monitor.dashboard_data import DashboardDataError, DashboardRepository
from usd1_monitor.dashboard_server import (
    DASHBOARD_HOST,
    create_dashboard_app,
    run_dashboard,
)
from usd1_monitor.models import Observation, RiskLevel


SNAPSHOT = {
    "generated_at": "2026-09-10T16:00:00+08:00",
    "business": {"level": "GREEN", "items": []},
    "health": {"level": "GREEN", "items": [], "collectors": []},
    "metrics": {},
    "recent": {"alerts": [], "chain_events": [], "announcements": []},
}


class FakeRepository:
    async def snapshot(self):
        return SNAPSHOT


@pytest.mark.asyncio
async def test_dashboard_api_and_healthz_succeed() -> None:
    app = create_dashboard_app(FakeRepository())
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.get("/api/dashboard")
        assert response.status == 200
        assert await response.json() == SNAPSHOT
        assert response.headers["Cache-Control"] == "no-store"

        health = await client.get("/healthz")
        assert health.status == 200
        assert await health.json() == {"status": "ok"}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_dashboard_api_returns_fixed_503_for_data_error() -> None:
    class BrokenRepository:
        async def snapshot(self):
            raise DashboardDataError("secret database path")

    client = TestClient(TestServer(create_dashboard_app(BrokenRepository())))
    await client.start_server()
    try:
        response = await client.get("/api/dashboard")
        body = await response.text()
        assert response.status == 503
        assert await response.json() == {
            "error": "dashboard_data_unavailable",
            "message": "监控数据暂时无法读取",
        }
        assert "secret database path" not in body
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_dashboard_rejects_post_and_sets_security_headers() -> None:
    app = create_dashboard_app(FakeRepository())
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post("/api/dashboard")
        assert response.status == 405
        for path in ("/api/dashboard", "/healthz"):
            current = await client.get(path)
            assert current.headers["Content-Security-Policy"] == (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
                "base-uri 'none'; frame-ancestors 'none'"
            )
            assert current.headers["X-Content-Type-Options"] == "nosniff"
            assert current.headers["Referrer-Policy"] == "no-referrer"
            assert current.headers["Cache-Control"] == "no-store"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_dashboard_serves_only_named_static_assets() -> None:
    app = create_dashboard_app(FakeRepository())
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        expected = {
            "/": "text/html",
            "/assets/dashboard.css": "text/css",
            "/assets/dashboard.js": "text/javascript",
        }
        for path, content_type in expected.items():
            response = await client.get(path)
            assert response.status == 200
            assert response.content_type == content_type
            assert response.headers["Cache-Control"] == "no-store"
        assert (await client.get("/favicon.ico")).status == 204
        assert (await client.get("/assets/../config.py")).status == 404
        assert (await client.get("/assets/unknown.js")).status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_run_dashboard_uses_fixed_host_and_configured_port(
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    class Repository:
        def __init__(self, path, *, timezone_name, event_active_seconds):
            captured["repository"] = (path, timezone_name, event_active_seconds)

        async def open(self):
            captured["opened"] = True

        async def close(self):
            captured["closed"] = True

    class Runner:
        def __init__(self, app):
            captured["app"] = app

        async def setup(self):
            captured["setup"] = True

        async def cleanup(self):
            captured["cleanup"] = True

    class Site:
        def __init__(self, runner, host, port):
            captured["site"] = (runner, host, port)

        async def start(self):
            captured["started"] = True

    stop_event = asyncio.Event()
    stop_event.set()
    config = AppConfig(
        database_path=tmp_path / "monitor.db",
        dashboard={"port": 8765},
    )

    await run_dashboard(
        config,
        repository_factory=Repository,
        runner_factory=Runner,
        site_factory=Site,
        stop_event=stop_event,
    )

    assert DASHBOARD_HOST == "127.0.0.1"
    assert captured["repository"] == (
        config.database_path,
        config.timezone,
        config.event_active_seconds,
    )
    assert captured["site"][1:] == ("127.0.0.1", 8765)
    assert captured["opened"] is True
    assert captured["setup"] is True
    assert captured["started"] is True
    assert captured["cleanup"] is True
    assert captured["closed"] is True


@pytest.mark.asyncio
async def test_dashboard_http_reads_existing_database_without_mutation(
    storage,
) -> None:
    now = datetime(2026, 9, 10, 8, 0, tzinfo=UTC)
    await storage.set_risk_state("market.price", RiskLevel.YELLOW, now, now)
    await storage.set_risk_state("health.por", RiskLevel.GREEN, now, now)
    await storage.insert_observation(
        Observation(
            "market.mid_price",
            "binance",
            "USD1USDT",
            0.998,
            "USDT",
            now,
            now,
        )
    )
    database_path = storage.path
    await storage.close()
    before = hashlib.sha256(database_path.read_bytes()).hexdigest()
    repository = DashboardRepository(database_path)
    await repository.open()
    client = TestClient(TestServer(create_dashboard_app(repository)))
    await client.start_server()
    try:
        response = await client.get("/api/dashboard")
        payload = await response.json()
        assert response.status == 200
        assert payload["business"]["level"] == "YELLOW"
        assert payload["health"]["level"] == "GREEN"
        assert payload["metrics"]["price_usd1usdt"]["value"] == 0.998
    finally:
        await client.close()
        await repository.close()

    after = hashlib.sha256(database_path.read_bytes()).hexdigest()
    assert after == before
