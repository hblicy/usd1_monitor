from pathlib import Path


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
