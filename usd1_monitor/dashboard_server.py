from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from aiohttp import web

from usd1_monitor.config import AppConfig
from usd1_monitor.dashboard_data import DashboardDataError, DashboardRepository


logger = logging.getLogger(__name__)

DASHBOARD_HOST = "127.0.0.1"
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
    "base-uri 'none'; frame-ancestors 'none'"
)


class SnapshotRepository(Protocol):
    async def snapshot(self) -> dict[str, object]: ...


@web.middleware
async def security_headers(
    request: web.Request, handler: Callable[[web.Request], Any]
) -> web.StreamResponse:
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        _set_security_headers(exc)
        raise
    _set_security_headers(response)
    return response


def _set_security_headers(response: web.StreamResponse) -> None:
    response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"


def create_dashboard_app(repository: SnapshotRepository) -> web.Application:
    static_root = Path(__file__).with_name("dashboard_static")

    async def dashboard_api(request: web.Request) -> web.Response:
        try:
            snapshot = await repository.snapshot()
        except DashboardDataError as exc:
            logger.warning(
                "dashboard data unavailable error=%s", type(exc).__name__
            )
            return web.json_response(
                {
                    "error": "dashboard_data_unavailable",
                    "message": "监控数据暂时无法读取",
                },
                status=503,
            )
        return web.json_response(snapshot)

    async def healthz(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def index(request: web.Request) -> web.FileResponse:
        return web.FileResponse(static_root / "index.html")

    async def dashboard_css(request: web.Request) -> web.FileResponse:
        return web.FileResponse(static_root / "dashboard.css")

    async def dashboard_js(request: web.Request) -> web.FileResponse:
        return web.FileResponse(static_root / "dashboard.js")

    async def favicon(request: web.Request) -> web.Response:
        return web.Response(status=204)

    app = web.Application(middlewares=[security_headers])
    app.router.add_get("/", index)
    app.router.add_get("/assets/dashboard.css", dashboard_css)
    app.router.add_get("/assets/dashboard.js", dashboard_js)
    app.router.add_get("/favicon.ico", favicon)
    app.router.add_get("/api/dashboard", dashboard_api)
    app.router.add_get("/healthz", healthz)
    return app


async def run_dashboard(
    config: AppConfig,
    *,
    repository_factory: Callable[..., DashboardRepository] = DashboardRepository,
    runner_factory: Callable[[web.Application], Any] = web.AppRunner,
    site_factory: Callable[..., Any] = web.TCPSite,
    stop_event: asyncio.Event | None = None,
) -> None:
    repository = repository_factory(
        config.database_path,
        timezone_name=config.timezone,
        event_active_seconds=config.event_active_seconds,
        coverage_max_age_seconds=config.por.coverage_max_age_seconds,
    )
    runner = None
    await repository.open()
    try:
        app = create_dashboard_app(repository)
        runner = runner_factory(app)
        await runner.setup()
        site = site_factory(runner, DASHBOARD_HOST, config.dashboard.port)
        await site.start()
        await (stop_event or asyncio.Event()).wait()
    finally:
        if runner is not None:
            await runner.cleanup()
        await repository.close()
