"use strict";

const REFRESH_MS = 30000;
const REQUEST_TIMEOUT_MS = 10000;

const LEVELS = {
  GREEN: { label: "正常 GREEN", business: "当前未发现影响 USD1 稳定性的重大风险。", health: "监控服务和数据源运行正常。" },
  YELLOW: { label: "注意 YELLOW", business: "发现需要关注的 USD1 业务风险。", health: "部分监控项存在异常，需要持续关注。" },
  RED: { label: "异常 RED", business: "发现严重的 USD1 业务风险，请立即检查。", health: "监控服务存在严重异常，请立即检查。" },
  UNKNOWN: { label: "未知 UNKNOWN", business: "暂无足够数据判断 USD1 风险。", health: "暂无足够数据判断监控健康状态。" },
};

const METRICS = [
  ["price_usd1usdt", "USD1/USDT", "price"],
  ["price_usd1usdc", "USD1/USDC", "price"],
  ["exit_usd1usdt_1m", "百万美元退出价（USDT）", "price"],
  ["exit_usd1usdc_1m", "百万美元退出价（USDC）", "price"],
  ["reserves", "储备金额", "amount"],
  ["multichain_supply", "完整多链供应量", "amount"],
  ["estimated_collateralization", "估算储备覆盖率", "ratio"],
  ["supply_change_24h", "24 小时供应量变化", "change-percent"],
  ["bridged_total", "桥接发行量", "amount"],
  ["locked_total", "桥锁定量", "amount"],
  ["bridge_delta", "桥接差额", "amount"],
];

const COLLECTOR_LABELS = {
  binance_market: "Binance 市场",
  evm_ethereum: "Ethereum 合约权限",
  evm_bsc: "BNB Chain 合约权限",
  por: "储备证明",
  supply: "供应量汇总",
  native_supply: "多链供应量",
  official_binance: "Binance 公告",
  official_bitgo: "BitGo 公告",
  official_wlfi: "WLFI 公告",
  official_occ: "OCC 公告",
};

let lastSnapshot = null;
let refreshInFlight = false;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function clear(node) {
  node.replaceChildren();
}

function formatTime(value) {
  if (!value) return "时间未知";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "时间未知";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(parsed);
}

function safeLink(value, label) {
  if (!value) return null;
  try {
    const url = new URL(value);
    if (!['http:', 'https:'].includes(url.protocol)) return null;
    const link = element("a", "source-link", label);
    link.href = url.href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    return link;
  } catch (error) {
    return null;
  }
}

function renderStatus(kind, data) {
  const safeData = data || { level: "UNKNOWN", items: [] };
  const level = LEVELS[safeData.level] ? safeData.level : "UNKNOWN";
  const panel = document.getElementById(`${kind}-status`);
  panel.dataset.level = level;
  document.getElementById(`${kind}-level`).textContent = LEVELS[level].label;
  document.getElementById(`${kind}-message`).textContent = LEVELS[level][kind];
}

function renderRiskItem(item) {
  const article = element("article", "risk-item");
  article.dataset.level = item.level || "YELLOW";
  article.append(element("span", "risk-dot"));
  const body = element("div");
  body.append(element("p", "risk-summary", item.summary || "监控状态发生变化"));
  body.append(element("small", "", `最近变化：${formatTime(item.changed_at)}`));
  article.append(body);
  return article;
}

function renderActiveRisks(snapshot) {
  const container = document.getElementById("active-risk-list");
  clear(container);
  const groups = [
    ["资产风险", snapshot.business?.items || []],
    ["数据与监控异常", snapshot.health?.items || []],
  ];
  if (groups.every(([, items]) => items.length === 0)) {
    const unknown = snapshot.business?.level === "UNKNOWN" || snapshot.health?.level === "UNKNOWN";
    container.append(element("p", "empty-copy", unknown ? "监控状态尚未完整生成。" : "当前未发现业务风险或监控异常。"));
    return;
  }
  for (const [title, items] of groups) {
    if (items.length === 0) continue;
    const group = element("section", "risk-group");
    group.append(element("h3", "risk-group-title", title));
    for (const item of items) group.append(renderRiskItem(item));
    container.append(group);
  }
}

function formatMetric(item, kind) {
  if (!item || typeof item.value !== "number") return "暂无数据";
  if (kind === "price") {
    return new Intl.NumberFormat("zh-CN", { minimumFractionDigits: 4, maximumFractionDigits: 6 }).format(item.value);
  }
  if (kind === "ratio") {
    const value = item.unit === "ratio" ? item.value * 100 : item.value;
    return `${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value)}%`;
  }
  if (kind === "change-percent") {
    return `${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2, signDisplay: "exceptZero" }).format(item.value)}%`;
  }
  const value = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(item.value);
  return item.unit ? `${value} ${item.unit}` : value;
}

function renderMetrics(metrics) {
  const container = document.getElementById("metric-grid");
  clear(container);
  for (const [key, label, kind] of METRICS) {
    const item = metrics?.[key] || null;
    const card = element("article", "metric-item");
    card.append(element("p", "metric-label", label));
    card.append(element("p", "metric-value", formatMetric(item, kind)));
    let metadata = "暂无数据";
    if (item) {
      const fill = Object.hasOwn(item, "fully_fillable") ? (item.fully_fillable ? " · 可完全成交" : " · 深度不足") : "";
      metadata = `${item.quality || "质量未知"} · ${formatTime(item.observed_at)}${fill}`;
    }
    card.append(element("p", "metric-meta", metadata));
    container.append(card);
  }
}

function renderCollectors(collectors) {
  const container = document.getElementById("collector-list");
  clear(container);
  if (!collectors || collectors.length === 0) {
    container.append(element("p", "empty-copy", "暂无数据源健康记录"));
    return;
  }
  for (const collector of collectors) {
    const failing = Number(collector.consecutive_failures) > 0;
    const row = element("article", "collector-item");
    row.dataset.failing = String(failing);
    const body = element("div");
    body.append(element("p", "collector-name", COLLECTOR_LABELS[collector.collector_id] || collector.collector_id));
    const lastTime = failing ? collector.last_failure_at : collector.last_success_at;
    body.append(element("p", "collector-detail", `${failing ? "最近失败" : "最近成功"}：${formatTime(lastTime)}`));
    if (failing && collector.last_error) body.append(element("p", "collector-error", collector.last_error));
    const state = element("div", "collector-state");
    state.append(element("span", "collector-dot"));
    state.append(element("span", "", failing ? `失败 ${collector.consecutive_failures} 次` : "正常"));
    row.append(body, state);
    container.append(row);
  }
}

function emptyRecent(container, text) {
  container.append(element("p", "empty-copy", text));
}

function renderAlerts(items) {
  const container = document.getElementById("recent-alerts");
  clear(container);
  if (!items || items.length === 0) return emptyRecent(container, "暂无告警记录");
  for (const item of items) {
    const row = element("div", "recent-item");
    row.append(element("p", "", item.content || "监控状态发生变化"));
    row.append(element("small", "", `${formatTime(item.created_at)} · ${item.status || "状态未知"}`));
    container.append(row);
  }
}

function renderChainEvents(items) {
  const container = document.getElementById("recent-chain-events");
  clear(container);
  if (!items || items.length === 0) return emptyRecent(container, "暂无链上事件");
  for (const item of items) {
    const row = element("div", "recent-item");
    row.append(element("p", "", `${item.chain || "未知网络"} · ${item.event_type || "链上事件"} · 区块 ${item.block_number ?? "未知"}`));
    row.append(element("small", "", formatTime(item.observed_at)));
    const link = safeLink(item.url, "查看链上交易");
    if (link) row.append(link);
    container.append(row);
  }
}

function renderAnnouncements(items) {
  const container = document.getElementById("recent-announcements");
  clear(container);
  if (!items || items.length === 0) return emptyRecent(container, "暂无官方公告");
  for (const item of items) {
    const row = element("div", "recent-item");
    row.append(element("p", "", item.title || "官方公告"));
    row.append(element("small", "", `${item.source || "官方来源"} · ${formatTime(item.published_at || item.first_seen_at)}`));
    const link = safeLink(item.url, "查看原文");
    if (link) row.append(link);
    container.append(row);
  }
}

function renderSnapshot(snapshot) {
  renderStatus("business", snapshot.business);
  renderStatus("health", snapshot.health);
  renderActiveRisks(snapshot);
  renderMetrics(snapshot.metrics);
  renderCollectors(snapshot.health?.collectors || []);
  renderAlerts(snapshot.recent?.alerts || []);
  renderChainEvents(snapshot.recent?.chain_events || []);
  renderAnnouncements(snapshot.recent?.announcements || []);
  document.getElementById("last-updated").textContent = `最后更新：${formatTime(snapshot.generated_at)}（北京时间）`;
  document.getElementById("dashboard").setAttribute("aria-busy", "false");
}

function renderUnavailable() {
  renderStatus("business", { level: "UNKNOWN" });
  renderStatus("health", { level: "UNKNOWN" });
  document.getElementById("business-message").textContent = "数据暂时无法读取，不能判断 USD1 风险。";
  document.getElementById("health-message").textContent = "数据暂时无法读取，不能判断监控健康。";
  document.getElementById("last-updated").textContent = "数据暂时无法读取";
  renderActiveRisks({ business: { level: "UNKNOWN", items: [] }, health: { level: "UNKNOWN", items: [] } });
  renderMetrics({});
  renderCollectors([]);
  renderAlerts([]);
  renderChainEvents([]);
  renderAnnouncements([]);
  document.getElementById("dashboard").setAttribute("aria-busy", "false");
}

async function refreshDashboard() {
  if (refreshInFlight) return;
  refreshInFlight = true;
  const button = document.getElementById("refresh-button");
  const banner = document.getElementById("stale-banner");
  button.disabled = true;
  button.textContent = "刷新中";
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch("/api/dashboard", { cache: "no-store", signal: controller.signal });
    if (!response.ok) throw new Error("dashboard request failed");
    const snapshot = await response.json();
    renderSnapshot(snapshot);
    lastSnapshot = snapshot;
    banner.hidden = true;
  } catch (error) {
    banner.hidden = false;
    if (!lastSnapshot) renderUnavailable();
  } finally {
    window.clearTimeout(timeout);
    button.disabled = false;
    button.textContent = window.innerWidth <= 420 ? "刷新" : "立即刷新";
    refreshInFlight = false;
  }
}

document.getElementById("refresh-button").addEventListener("click", refreshDashboard);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") refreshDashboard();
});
window.addEventListener("resize", () => {
  document.getElementById("refresh-button").textContent = window.innerWidth <= 420 ? "刷新" : "立即刷新";
});

renderMetrics({});
refreshDashboard();
window.setInterval(refreshDashboard, REFRESH_MS);
