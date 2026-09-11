import json
from pathlib import Path
import shutil
import subprocess

import pytest


ASSET_ROOT = Path(__file__).parents[1] / "usd1_monitor" / "dashboard_static"


def test_dashboard_assets_have_required_structure_and_no_external_dependencies() -> None:
    html = (ASSET_ROOT / "index.html").read_text(encoding="utf-8")
    css = (ASSET_ROOT / "dashboard.css").read_text(encoding="utf-8")
    javascript = (ASSET_ROOT / "dashboard.js").read_text(encoding="utf-8")

    assert '<meta name="viewport"' in html
    assert 'id="business-status"' in html
    assert 'id="health-status"' in html
    assert 'id="active-risks"' in html
    assert 'id="key-metrics"' in html
    assert 'id="collector-health"' in html
    assert 'id="recent-events"' in html
    assert "正在读取监控数据" in html
    assert "30000" in javascript
    assert "textContent" in javascript
    assert "innerHTML" not in javascript
    assert "AbortController" in javascript
    assert "visibilitychange" in javascript
    assert "@media" in css
    assert "min-height: 44px" in css
    assert "https://" not in html + css + javascript


def test_dashboard_assets_include_accessible_responsive_states() -> None:
    html = (ASSET_ROOT / "index.html").read_text(encoding="utf-8")
    css = (ASSET_ROOT / "dashboard.css").read_text(encoding="utf-8")

    assert 'aria-live="polite"' in html
    assert ':focus-visible' in css
    assert 'prefers-reduced-motion: reduce' in css
    assert "720px" in css
    assert ".page-header > div {\n  min-width: 0;" in css
    assert ".risk-item > div {\n  min-width: 0;" in css


def test_dashboard_assets_distinguish_ratio_changes_and_risk_groups() -> None:
    css = (ASSET_ROOT / "dashboard.css").read_text(encoding="utf-8")
    javascript = (ASSET_ROOT / "dashboard.js").read_text(encoding="utf-8")

    assert '["estimated_collateralization", "估算储备覆盖率", "ratio"]' in javascript
    assert '["supply_change_24h", "24 小时供应量变化", "change-percent"]' in javascript
    assert "资产风险" in javascript
    assert "数据与监控异常" in javascript
    assert ".risk-group" in css


def test_dashboard_assets_explain_unavailable_supply_metrics() -> None:
    javascript = (ASSET_ROOT / "dashboard.js").read_text(encoding="utf-8")

    assert "function unavailableMetricReason" in javascript
    assert "等待完整多链供应量采集" in javascript
    assert "等待官方储备数据更新" in javascript
    assert "正在积累24小时完整数据" in javascript
    assert "renderMetrics(snapshot.metrics, snapshot.generated_at)" in javascript


def test_unavailable_metric_reason_checks_supply_and_reserve_freshness() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for dashboard behavior tests")
    javascript = (ASSET_ROOT / "dashboard.js").read_text(encoding="utf-8")
    start_marker = (
        "function isFreshMetric"
        if "function isFreshMetric" in javascript
        else "function unavailableMetricReason"
    )
    start = javascript.index(start_marker)
    end = javascript.index("function renderMetrics", start)
    metric_logic = javascript[start:end]
    generated_at = "2026-09-11T03:00:00+00:00"
    fresh_supply = {
        "value": 4_200_000_000,
        "observed_at": "2026-09-11T02:30:00+00:00",
    }
    stale_supply = {
        "value": 4_200_000_000,
        "observed_at": "2026-09-11T01:00:00+00:00",
    }
    fresh_reserves = {
        "value": 4_278_303_267,
        "observed_at": "2026-09-11T02:30:00+00:00",
    }
    stale_reserves = {
        "value": 4_278_303_267,
        "observed_at": "2026-09-11T01:00:00+00:00",
    }
    cases = [
        {
            "key": "estimated_collateralization",
            "metrics": {
                "multichain_supply": stale_supply,
                "reserves": fresh_reserves,
            },
            "generatedAt": generated_at,
        },
        {
            "key": "supply_change_24h",
            "metrics": {"multichain_supply": stale_supply},
            "generatedAt": generated_at,
        },
        {
            "key": "estimated_collateralization",
            "metrics": {
                "multichain_supply": fresh_supply,
                "reserves": stale_reserves,
            },
            "generatedAt": generated_at,
        },
        {
            "key": "estimated_collateralization",
            "metrics": {
                "multichain_supply": fresh_supply,
                "reserves": fresh_reserves,
            },
            "generatedAt": generated_at,
        },
        {
            "key": "supply_change_24h",
            "metrics": {"multichain_supply": fresh_supply},
            "generatedAt": generated_at,
        },
    ]
    script = "\n".join(
        (
            "const METRIC_FRESH_MS = 4500 * 1000;",
            metric_logic,
            f"const cases = {json.dumps(cases)};",
            "process.stdout.write(JSON.stringify(cases.map((item) => "
            "unavailableMetricReason(item.key, item.metrics, item.generatedAt))));",
        )
    )

    result = subprocess.run(
        [node, "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert json.loads(result.stdout) == [
        "等待完整多链供应量采集",
        "等待完整多链供应量采集",
        "等待官方储备数据更新",
        "等待下一次覆盖率计算",
        "正在积累24小时完整数据",
    ]


def test_supply_metric_availability_rejects_stale_values() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for dashboard behavior tests")
    javascript = (ASSET_ROOT / "dashboard.js").read_text(encoding="utf-8")
    start = javascript.index("function isFreshMetric")
    end = javascript.index("function renderMetrics", start)
    metric_logic = javascript[start:end]
    generated_at = "2026-09-11T03:00:00+00:00"
    fresh = {
        "value": 4_200_000_000,
        "observed_at": "2026-09-11T02:30:00+00:00",
    }
    stale = {
        "value": 4_200_000_000,
        "observed_at": "2026-09-11T01:00:00+00:00",
    }
    cases = [
        ["multichain_supply", stale, {}],
        ["bridged_total", stale, {}],
        ["locked_total", stale, {}],
        ["bridge_delta", stale, {}],
        [
            "estimated_collateralization",
            fresh,
            {"multichain_supply": stale, "reserves": fresh},
        ],
        [
            "estimated_collateralization",
            fresh,
            {"multichain_supply": fresh, "reserves": stale},
        ],
        ["supply_change_24h", fresh, {"multichain_supply": stale}],
        ["multichain_supply", fresh, {}],
        [
            "estimated_collateralization",
            fresh,
            {"multichain_supply": fresh, "reserves": fresh},
        ],
        ["supply_change_24h", fresh, {"multichain_supply": fresh}],
        ["reserves", stale, {}],
    ]
    script = "\n".join(
        (
            "const METRIC_FRESH_MS = 4500 * 1000;",
            metric_logic,
            f"const cases = {json.dumps(cases)};",
            f"const generatedAt = {json.dumps(generated_at)};",
            "process.stdout.write(JSON.stringify(cases.map(([key, item, metrics]) => "
            "isAvailableMetric(key, item, metrics, generatedAt))));",
        )
    )

    result = subprocess.run(
        [node, "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert json.loads(result.stdout) == [
        False,
        False,
        False,
        False,
        False,
        False,
        False,
        True,
        True,
        True,
        True,
    ]
