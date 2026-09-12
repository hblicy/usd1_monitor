from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import usd1_monitor.cli as cli_module
from usd1_monitor.cli import async_main, build_market_monitor
from usd1_monitor.collectors.announcements import BinancePartialCollectionError
from usd1_monitor.collectors.multichain_supply import REQUIRED_COMPONENT_IDS
from usd1_monitor.config import AppConfig
from usd1_monitor.models import Announcement
from usd1_monitor.scheduler import CheckResult, NOT_MONITORED


class FakeMonitor:
    def __init__(self, success: bool) -> None:
        self.success = success
        self.deliver_arguments: list[bool] = []

    async def check_once(self, *, deliver: bool = True) -> CheckResult:
        self.deliver_arguments.append(deliver)
        return CheckResult(self.success, () if self.success else ("binance failed",))


class FakeHttp:
    async def close(self) -> None:
        return None


def test_builder_wires_all_multichain_supply_sources(
    storage,
    tmp_path: Path,
) -> None:
    config = AppConfig(database_path=tmp_path / "monitor.db")

    monitor, resource = build_market_monitor(config, storage)

    source = monitor._reserve_supply._supply._multichain
    assert source.required_component_ids == REQUIRED_COMPONENT_IDS
    assert source.max_concurrency == 4
    assert resource is not None


def test_builder_reuses_evm_and_solana_rpc_clients_for_custody(
    storage,
    tmp_path: Path,
) -> None:
    config = AppConfig.model_validate(
        {
            "database_path": tmp_path / "monitor.db",
            "custody": {
                "addresses": [
                    {
                        "chain": "ethereum",
                        "address": "0x" + "11" * 20,
                        "entity": "binance_cex",
                        "label": "Binance",
                        "role": "hot_wallet",
                    }
                ]
            },
        }
    )

    monitor, resource = build_market_monitor(config, storage)

    assert monitor._custody is not None
    ethereum_rpc = monitor._evm_chains[0]._scanner._rpc
    assert monitor._custody._collector._evm_rpcs["ethereum"] is ethereum_rpc
    supply_sources = monitor._reserve_supply._supply._multichain._sources
    solana_source = next(
        item for item in supply_sources if "native_solana" in item.component_ids
    )
    assert monitor._custody._collector._solana_rpc is solana_source._rpc
    assert resource is not None


def write_config(path: Path, database_path: Path) -> None:
    path.write_text(
        f"database_path: '{database_path.as_posix()}'\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_invalid_configuration_exits_two(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("market: []\n", encoding="utf-8")

    assert await async_main(["--config", str(config), "check"]) == 2


@pytest.mark.asyncio
async def test_dashboard_command_does_not_open_writable_storage(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    database_path = tmp_path / "missing.db"
    write_config(config_path, database_path)
    captured: list[AppConfig] = []

    async def dashboard_runner(config: AppConfig) -> None:
        captured.append(config)

    result = await async_main(
        ["--config", str(config_path), "dashboard"],
        dashboard_runner=dashboard_runner,
    )

    assert result == 0
    assert captured[0].database_path == database_path
    assert database_path.exists() is False


@pytest.mark.asyncio
@pytest.mark.parametrize(("success", "exit_code"), [(True, 0), (False, 1)])
async def test_check_exit_code_and_no_delivery(
    tmp_path: Path, success: bool, exit_code: int
) -> None:
    config = tmp_path / "config.yaml"
    write_config(config, tmp_path / "monitor.db")
    monitor = FakeMonitor(success)

    def builder(app_config, storage):
        return monitor, FakeHttp()

    result = await async_main(
        ["--config", str(config), "check"], monitor_builder=builder
    )

    assert result == exit_code
    assert monitor.deliver_arguments == [False]


@pytest.mark.asyncio
async def test_status_lists_every_not_monitored_item(tmp_path: Path, capsys) -> None:
    config = tmp_path / "config.yaml"
    write_config(config, tmp_path / "monitor.db")

    assert await async_main(["--config", str(config), "status"]) == 0
    output = capsys.readouterr().out
    assert all(item in output for item in NOT_MONITORED)


@pytest.mark.asyncio
async def test_status_passes_configured_por_coverage_freshness(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = tmp_path / "config.yaml"
    database_path = tmp_path / "monitor.db"
    config.write_text(
        (
            f"database_path: '{database_path.as_posix()}'\n"
            "por:\n"
            "  coverage_max_age_seconds: 3600\n"
        ),
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    async def fake_print_status(
        storage,
        *,
        timezone_name,
        coverage_max_age_seconds,
        now=None,
    ) -> None:
        captured["coverage_max_age_seconds"] = coverage_max_age_seconds

    monkeypatch.setattr(cli_module, "_print_status", fake_print_status)

    assert await async_main(["--config", str(config), "status"]) == 0
    assert captured["coverage_max_age_seconds"] == 3600


@pytest.mark.asyncio
async def test_cli_loads_adjacent_dotenv_without_overriding_environment(
    tmp_path: Path, monkeypatch
) -> None:
    config = tmp_path / "config.yaml"
    write_config(config, tmp_path / "monitor.db")
    (tmp_path / ".env").write_text(
        "WECHAT_WEBHOOK=https://qyapi.weixin.qq.com/from-file\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "WECHAT_WEBHOOK", "https://qyapi.weixin.qq.com/from-environment"
    )
    captured = {}

    def builder(app_config, storage):
        captured["webhook"] = app_config.wechat_webhook
        return FakeMonitor(True), FakeHttp()

    assert await async_main(
        ["--config", str(config), "check"], monitor_builder=builder
    ) == 0
    assert captured["webhook"] == "https://qyapi.weixin.qq.com/from-environment"


@pytest.mark.asyncio
async def test_cli_loads_webhook_from_adjacent_dotenv(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config.yaml"
    write_config(config, tmp_path / "monitor.db")
    (tmp_path / ".env").write_text(
        "WECHAT_WEBHOOK=https://qyapi.weixin.qq.com/from-file\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("WECHAT_WEBHOOK", raising=False)
    captured = {}

    def builder(app_config, storage):
        captured["webhook"] = app_config.wechat_webhook
        return FakeMonitor(True), FakeHttp()

    assert await async_main(
        ["--config", str(config), "check"], monitor_builder=builder
    ) == 0
    assert captured["webhook"] == "https://qyapi.weixin.qq.com/from-file"


@pytest.mark.asyncio
async def test_default_builder_wires_binance_persistence_providers(
    storage,
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 9, 12, tzinfo=UTC)
    release_ms = int(now.timestamp() * 1000)
    for source, stable_id, metadata in (
        ("binance", "known", {"body_text": "USD1 prior body"}),
        ("binance_scan", "recent-neutral", {"usd1_relevant": False}),
        (
            "binance_scan",
            "recent-failure",
            {"scan_error": "TimeoutError", "usd1_relevant": False},
        ),
    ):
        await storage.upsert_announcement(
            Announcement(
                source,
                stable_id,
                "General service update",
                f"https://www.binance.com/{stable_id}",
                now,
                stable_id,
                now,
                metadata,
            )
        )
    await storage.upsert_announcement(
        Announcement(
            "binance_scan",
            "expired-neutral",
            "General service update",
            "https://www.binance.com/expired-neutral",
            now,
            "expired-neutral",
            now - timedelta(days=2),
            {"usd1_relevant": False},
        )
    )

    entries = [
        {
            "code": stable_id,
            "title": "General service update",
            "releaseDate": release_ms - index,
        }
        for index, stable_id in enumerate(
            (
                "known",
                "recent-neutral",
                "recent-failure",
                "expired-neutral",
                "new-neutral",
            )
        )
    ]

    class BinanceHttp:
        detail_calls: list[str] = []

        async def get_json(self, url: str, params=None):
            if "detail/query" not in url:
                return {
                    "code": "000000",
                    "data": {"catalogs": [{"articles": entries}]},
                }
            stable_id = params["articleCode"]
            self.detail_calls.append(stable_id)
            body = (
                "USD1 withdrawals are restricted"
                if stable_id == "known"
                else "ABC service update"
            )
            return {
                "code": "000000",
                "data": {"body": f"<main>{body}</main>"},
            }

    monitor, real_http = build_market_monitor(
        AppConfig(database_path=tmp_path / "unused.db"),
        storage,
    )
    information = monitor._information
    assert information is not None
    collector = information._sources["binance"]
    fake_http = BinanceHttp()
    collector._http = fake_http

    try:
        with pytest.raises(BinancePartialCollectionError) as error:
            await collector.collect(now)
    finally:
        await real_http.close()

    assert [item.stable_id for item in error.value.items] == ["known"]
    assert fake_http.detail_calls == ["known", "new-neutral", "expired-neutral"]
    assert await storage.announcement_stable_ids("binance_scan") == {
        "recent-neutral",
        "recent-failure",
        "expired-neutral",
        "new-neutral",
    }


@pytest.mark.asyncio
async def test_default_builder_does_not_enable_privileged_block_scanner(
    storage, tmp_path: Path
) -> None:
    monitor, http = build_market_monitor(
        AppConfig(database_path=tmp_path / "unused.db"), storage
    )

    try:
        assert all(
            chain._privileged_collector is None
            for chain in monitor._evm_chains
        )
    finally:
        await http.close()
