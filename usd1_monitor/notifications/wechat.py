from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Iterable, Protocol

from usd1_monitor.models import RiskLevel, RiskTransition
from usd1_monitor.time_utils import local_iso


class JsonPoster(Protocol):
    async def post_json(self, url: str, payload: dict) -> object: ...


class WeChatDeliveryError(RuntimeError):
    pass


WECHAT_TEXT_MAX_BYTES = 2048
LEVEL_LABELS = {
    RiskLevel.GREEN: "🟢 USD1 正常",
    RiskLevel.YELLOW: "🟡 USD1 注意",
    RiskLevel.RED: "🔴 USD1 危险",
}


def _level_heading(level: RiskLevel, *, recovered: bool) -> str:
    if recovered:
        return "🟢 USD1 已恢复正常"
    return LEVEL_LABELS[level]


def _display_time(value: datetime | str, timezone_name: str) -> str:
    localized = datetime.fromisoformat(local_iso(value, timezone_name))
    return f"{localized:%Y-%m-%d %H:%M:%S}（北京时间）"


def _source_urls(evidence: dict[str, object]) -> list[str]:
    raw_urls = evidence.get("source_urls")
    if isinstance(raw_urls, (list, tuple)):
        return [str(url) for url in raw_urls if url]
    source_url = evidence.get("source_url")
    return [str(source_url)] if source_url else []


def split_wechat_text(
    content: str, *, max_bytes: int = WECHAT_TEXT_MAX_BYTES
) -> list[str]:
    if max_bytes < 4:
        raise ValueError("max_bytes must fit one UTF-8 code point")
    if len(content.encode("utf-8")) <= max_bytes:
        return [content]

    chunks: list[str] = []
    current: list[str] = []
    current_bytes = 0
    for character in content:
        character_bytes = len(character.encode("utf-8"))
        if current and current_bytes + character_bytes > max_bytes:
            chunks.append("".join(current))
            current = []
            current_bytes = 0
        current.append(character)
        current_bytes += character_bytes
    if current:
        chunks.append("".join(current))
    return chunks


class WeChatNotifier:
    def __init__(self, webhook: str, http: JsonPoster) -> None:
        self._webhook = webhook
        self._http = http

    async def send_text(self, content: str) -> None:
        response = await self._http.post_json(
            self._webhook,
            {"msgtype": "text", "text": {"content": content}},
        )
        if not isinstance(response, dict) or response.get("errcode") != 0:
            errcode = response.get("errcode", "missing") if isinstance(response, dict) else "missing"
            errmsg = response.get("errmsg", "invalid response") if isinstance(response, dict) else "invalid response"
            raise WeChatDeliveryError(
                f"enterprise WeChat rejected message: errcode={errcode}, errmsg={errmsg}"
            )


def _evidence_line(
    transition: RiskTransition, *, timezone_name: str
) -> str:
    evidence = transition.evidence
    raw_data_time = evidence.get("data_time", transition.changed_at)
    try:
        data_time = local_iso(raw_data_time, timezone_name)
    except (TypeError, ValueError):
        data_time = str(raw_data_time)
    source_urls = evidence.get("source_urls")
    if isinstance(source_urls, (list, tuple)) and source_urls:
        source = ", ".join(str(url) for url in source_urls)
    else:
        source = evidence.get("source_url", "未提供")
    summary = [f"- {transition.rule_id}"]
    if "current" in evidence or "mids" in evidence:
        summary.append(f"当前值={evidence.get('current', evidence.get('mids'))}")
    if "threshold" in evidence or "yellow_threshold" in evidence:
        summary.append(
            "阈值="
            f"{evidence.get('threshold', evidence.get('yellow_threshold'))}"
        )
    summary.extend(
        (
            "首次触发="
            f"{local_iso(transition.first_triggered_at, timezone_name)}",
            f"数据时间={data_time}",
            f"来源={source}",
        )
    )
    line = ": ".join((summary[0], "; ".join(summary[1:])))
    detail_keys = (
        "fact_type",
        "chain",
        "event_key",
        "sender",
        "to",
        "account",
        "from_address",
        "to_address",
        "amount",
        "selector",
        "severe_threshold",
        "severe_duration_seconds",
        "exit_capacity",
        "removed_events",
        "from_block",
        "to_block",
        "last_error",
    )
    details = "; ".join(
        f"{key}={evidence[key]}"
        for key in detail_keys
        if evidence.get(key) is not None
    )
    return f"{line}; {details}" if details else line


def format_transitions(
    transitions: Iterable[RiskTransition],
    *,
    overall_level: RiskLevel | None = None,
    timezone_name: str = "Asia/Shanghai",
) -> str:
    items = list(transitions)
    if not items:
        raise ValueError("at least one transition is required")

    overall = (
        overall_level
        if overall_level is not None
        else max((item.current for item in items), default=RiskLevel.GREEN)
    )
    recovery_from = [
        item.previous
        for item in items
        if item.current is RiskLevel.GREEN and item.previous is not RiskLevel.GREEN
    ]
    transition_level = max(item.current for item in items)
    health_only = all(item.rule_id.startswith("health.") for item in items)
    if health_only:
        heading = f"USD1 监控健康：{overall.name}"
    elif transition_level > overall:
        heading = (
            f"USD1 风险事件：{transition_level.name}"
            f"（当前持续状态：{overall.name}）"
        )
    else:
        heading = f"USD1 风险状态：{overall.name}"
    if recovery_from:
        heading += f"（恢复，先前为 {max(recovery_from).name}）"

    grouped: dict[str, list[RiskTransition]] = defaultdict(list)
    for index, item in enumerate(items):
        group_key = item.cause_id or f"rule:{index}:{item.rule_id}"
        grouped[group_key].append(item)

    lines = [heading]
    for group_key, group_items in grouped.items():
        cause = group_items[0].cause_id
        if cause:
            lines.append(f"原因 {cause}")
        lines.extend(
            _evidence_line(item, timezone_name=timezone_name)
            for item in group_items
        )
    return "\n".join(lines)
