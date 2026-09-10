from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Iterable, Protocol

from usd1_monitor.engine.aggregate import is_monitoring_health_rule
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
CHAIN_LABELS = {"ethereum": "Ethereum", "bsc": "BNB Chain"}
COLLECTOR_LABELS = {
    "binance_market": "Binance 市场",
    "market": "Binance 市场",
    "evm_ethereum": "Ethereum",
    "evm_bsc": "BNB Chain",
    "por": "储备证明",
    "native_supply": "链上供应量",
    "defillama_supply": "全链供应量估算",
    "supply_ethereum": "Ethereum 供应量",
    "supply_bsc": "BNB Chain 供应量",
    "supply_defillama": "DefiLlama 全链供应量",
    "supply_multichain": "多链供应量",
    "supply": "供应量",
    "reserve_supply": "储备与供应量",
    "information": "官方信息",
    "official_binance": "Binance 官方公告",
    "official_bitgo": "BitGo 官方信息",
    "official_wlfi": "WLFI 官方信息",
    "official_occ": "OCC 官方信息",
    "notification_wechat": "企业微信通知",
}
FACT_LABELS = {
    "PAUSED": "USD1 合约已暂停",
    "UNPAUSED": "USD1 合约已恢复运行",
    "FREEZE": "地址被 USD1 合约冻结",
    "UNFREEZE": "地址已被 USD1 合约解除冻结",
    "IMPLEMENTATION_CHANGED": "USD1 实现合约发生变化",
    "ADMIN_CHANGED": "USD1 管理员地址发生变化",
    "ADMIN_OWNER_CHANGED": "USD1 代理管理员控制人发生变化",
    "CODE_HASH_CHANGED": "USD1 合约代码发生变化",
    "OWNER_CHANGED": "USD1 所有者地址发生变化",
    "PRIVILEGED_UNKNOWN_CALL": "检测到未知的高权限合约调用",
    "MINT": "检测到大额 USD1 铸造",
    "BURN": "检测到大额 USD1 销毁",
    "REORG_CORRECTION": "链上重组导致先前事件被修正",
}
NOT_MONITORED_LABELS = {
    "private_exchange_account": "私有交易所账户",
    "active_conversion_probe": "主动兑换测试",
    "tron_solana_aptos_tempo_bridges": "Tron、Solana、Aptos、Tempo 跨链桥",
    "binance_wallet_concentration": "Binance 钱包集中度",
    "social_media_sentiment": "社交媒体情绪",
    "defi_liquidations": "DeFi 清算",
    "web_dashboard": "网页仪表盘",
    "full_multichain_supply_reconciliation": "完整多链供应量核对",
}
OFFICIAL_SOURCE_LABELS = {
    "binance": "Binance",
    "bitgo": "BitGo",
    "wlfi": "WLFI",
    "occ": "OCC",
}
SUPPLY_COMPONENT_LABELS = {
    "native_ethereum": "Ethereum 原生供应量",
    "native_bsc": "BNB Chain 原生供应量",
    "native_tron": "Tron 原生供应量",
    "native_solana": "Solana 原生供应量",
    "native_aptos": "Aptos 原生供应量",
    "native_tempo": "Tempo 原生供应量",
    "bridged_plume": "Plume 跨链发行量",
    "bridged_ab": "AB Core 跨链发行量",
    "bridged_monad": "Monad 跨链发行量",
    "bridged_mantle": "Mantle 跨链发行量",
    "bridged_morph": "Morph 跨链发行量",
    "locked_ethereum": "Ethereum 桥池余额",
    "locked_bsc": "BNB Chain 桥池余额",
    "locked_solana": "Solana 桥池余额",
    "locked_aptos": "Aptos 桥池余额",
    "locked_tempo": "Tempo 桥池余额",
}


def _level_heading(
    level: RiskLevel,
    *,
    recovered: bool,
    health_only: bool = False,
    por_age_only: bool = False,
) -> str:
    if por_age_only:
        if recovered:
            return "🟢 USD1 储备数据已恢复更新"
        return f"{'🟡' if level is RiskLevel.YELLOW else '🔴'} USD1 储备数据更新延迟"
    if health_only:
        if recovered:
            return "🟢 USD1 监控已恢复"
        return f"{'🟡' if level is RiskLevel.YELLOW else '🔴'} USD1 监控异常"
    if recovered:
        return "🟢 USD1 已恢复正常"
    return LEVEL_LABELS[level]


def _display_time(value: datetime | str, timezone_name: str) -> str:
    localized = datetime.fromisoformat(local_iso(value, timezone_name))
    timezone_label = "北京时间" if timezone_name == "Asia/Shanghai" else timezone_name
    return f"{localized:%Y-%m-%d %H:%M:%S}（{timezone_label}）"


def _source_urls(evidence: dict[str, object]) -> list[str]:
    raw_urls = evidence.get("source_urls")
    if isinstance(raw_urls, (list, tuple)):
        return [str(url) for url in raw_urls if url]
    source_url = evidence.get("source_url")
    return [str(source_url)] if source_url else []


def format_startup_message(
    monitored: Iterable[str], not_monitored: Iterable[str]
) -> str:
    active = "、".join(dict.fromkeys(monitored))
    missing = "、".join(
        dict.fromkeys(
            NOT_MONITORED_LABELS.get(item, "其他未覆盖能力")
            for item in not_monitored
        )
    )
    return (
        "🟢 USD1 监控已启动\n\n"
        f"正在监控：\n{active}\n\n"
        f"暂未覆盖：\n{missing}"
    )


def _format_number(value: object) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:.8f}".rstrip("0").rstrip(".")
    return str(value)


def _format_usd1_amount(value: object) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return _format_number(value)
    amount = abs(float(value))
    if amount >= 100_000_000:
        return f"{_format_number(amount / 100_000_000)} 亿"
    if amount >= 10_000:
        return f"{_format_number(amount / 10_000)} 万"
    return _format_number(amount)


def _format_value(value: object) -> str:
    if isinstance(value, dict):
        return "、".join(
            f"{key} {_format_number(item)}" for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return "、".join(_format_number(item) for item in value)
    return _format_number(value)


def _format_amount_label(value: str) -> str:
    try:
        amount = int(value)
    except ValueError:
        return value
    if amount % 10_000 == 0:
        return f"{amount // 10_000} 万"
    return str(amount)


def _exit_sizes(evidence: dict[str, object]) -> list[str]:
    capacity = evidence.get("exit_capacity")
    if not isinstance(capacity, dict):
        return []
    sizes: set[str] = set()
    for symbol_capacity in capacity.values():
        if isinstance(symbol_capacity, dict):
            sizes.update(str(size) for size in symbol_capacity)
    return sorted(sizes, key=lambda item: int(item) if item.isdigit() else 0)


def _collector_label(collector_id: str) -> str:
    normalized = (
        collector_id.removeprefix("scheduler_")
        .removeprefix("collector_")
    )
    return COLLECTOR_LABELS.get(
        normalized,
        COLLECTOR_LABELS.get(collector_id, "监控数据源"),
    )


def _chain_label(rule_id: str, evidence: dict[str, object]) -> str:
    chain = str(evidence.get("chain", ""))
    if not chain:
        chain = "bsc" if ".bsc." in rule_id else "ethereum"
    return CHAIN_LABELS.get(chain, "链上")


def _human_summary(transition: RiskTransition) -> tuple[str, list[str]]:
    rule_id = transition.rule_id
    evidence = transition.evidence
    recovered = transition.current is RiskLevel.GREEN
    details: list[str] = []

    if rule_id == "health.supply_multichain":
        if recovered:
            return "供应量数据获取已恢复", details
        failed_sources = evidence.get("failed_sources")
        if isinstance(failed_sources, (list, tuple)):
            labels = list(
                dict.fromkeys(
                    SUPPLY_COMPONENT_LABELS.get(str(item), str(item))
                    for item in failed_sources
                )
            )
            if labels:
                details.append(f"未能获取：{'、'.join(labels)}")
        failures = evidence.get("current")
        if isinstance(failures, (int, float)) and failures > 0:
            return (
                "供应量数据连续 "
                f"{_format_number(failures)} 次未能完整获取"
            ), details
        return "供应量数据暂时未能完整获取", details

    if rule_id.startswith("health."):
        label = _collector_label(rule_id.removeprefix("health."))
        if recovered:
            return f"{label}数据获取已恢复", details
        failures = evidence.get("current")
        if isinstance(failures, (int, float)) and failures > 0:
            return f"{label}数据连续 {_format_number(failures)} 次未能获取", details
        return f"{label}数据暂时无法获取", details

    if rule_id == "market.price.severe":
        summary = "USD1 价格已恢复正常" if recovered else "USD1 价格持续严重偏离 1 美元"
        price = evidence.get("current", evidence.get("mids"))
        if price is not None:
            details.append(f"当前价格：{_format_value(price)}")
        threshold = evidence.get("severe_threshold")
        if threshold is not None:
            details.append(f"危险价格：{_format_number(threshold)}")
        duration = evidence.get("severe_duration_seconds")
        if isinstance(duration, (int, float)):
            details.append(f"持续条件：{_format_number(duration / 60)} 分钟")
        return summary, details

    if rule_id == "market.price":
        summary = "USD1 价格已恢复正常" if recovered else "USD1 价格低于预警线"
        price = evidence.get("current", evidence.get("mids"))
        if price is not None:
            details.append(f"当前价格：{_format_value(price)}")
        threshold = evidence.get("yellow_threshold", evidence.get("threshold"))
        if threshold is not None:
            details.append(f"预警价格：{_format_number(threshold)}")
        return summary, details

    if rule_id == "market.liquidity":
        summary = "USD1 市场流动性已恢复" if recovered else "USD1 市场流动性不足"
        threshold = evidence.get("threshold")
        if threshold is not None:
            details.append(f"最低可接受价格：{_format_number(threshold)}")
        sizes = _exit_sizes(evidence)
        if sizes:
            details.append(
                "已检查卖出规模："
                + "、".join(f"{_format_amount_label(size)} USD1" for size in sizes)
            )
        return summary, details

    if rule_id == "market.trading":
        summary = "USD1 交易状态已恢复" if recovered else "USD1 部分交易对已暂停交易"
        trading = evidence.get("trading")
        if isinstance(trading, dict):
            stopped = [str(symbol) for symbol, active in trading.items() if not active]
            if stopped:
                details.append(f"暂停交易：{'、'.join(stopped)}")
        return summary, details

    if rule_id == "por.age":
        if recovered:
            return "USD1 储备数据已恢复更新", details
        age = evidence.get("current")
        if isinstance(age, (int, float)) and age > 0:
            minutes = int(age / 60 + 0.5)
            summary = f"官方储备数据已有约 {minutes} 分钟未更新"
        else:
            summary = "官方储备数据暂未更新"
        details.append(
            "说明：这不代表储备不足，只表示目前无法获得最新储备信息。"
        )
        return summary, details

    if rule_id == "por.reserve_change":
        summary = "USD1 储备变化已恢复稳定" if recovered else "USD1 储备数量出现较大变化"
        current = evidence.get("current")
        if current is not None:
            details.append(f"当前储备：{_format_number(current)} USD1")
        return summary, details

    if rule_id in {"supply.estimated_coverage", "supply.estimated_collateralization"}:
        summary = "USD1 估算储备覆盖率已恢复" if recovered else "USD1 估算储备覆盖率异常"
        current = evidence.get("current")
        if current is not None:
            details.append(f"估算覆盖率：{_format_number(current)}%")
        return summary, details

    if rule_id == "supply.native_drop_24h":
        summary = "USD1 链上供应量已恢复稳定" if recovered else "USD1 链上供应量在 24 小时内明显下降"
        current = evidence.get("current")
        if isinstance(current, (int, float)):
            details.append(f"下降比例：{_format_number(current * 100)}%")
        return summary, details

    if rule_id == "supply.bridge_reconciliation":
        if recovered:
            summary = "跨链发行量与桥池锁仓量已恢复正常"
        else:
            difference = _format_usd1_amount(evidence.get("difference", 0))
            if evidence.get("direction") == "locked_excess":
                summary = f"桥池锁仓量比跨链发行量多 {difference} USD1"
            else:
                summary = f"跨链发行量比桥池锁仓量多 {difference} USD1"
        issued = evidence.get("issued")
        locked = evidence.get("locked")
        if issued is not None:
            details.append(
                f"跨链发行量：{_format_usd1_amount(issued)} USD1"
            )
        if locked is not None:
            details.append(
                f"桥池锁仓量：{_format_usd1_amount(locked)} USD1"
            )
        ratio = evidence.get("difference_percent")
        if isinstance(ratio, (int, float)):
            details.append(f"差额比例：{_format_number(abs(ratio))}%")
        return summary, details

    if rule_id.startswith("event.information.bitgo.attestation_fields."):
        if recovered:
            return "BitGo 鉴证报告的关键信息已确认稳定", details
        return "BitGo 鉴证报告的关键信息发生变化", details

    if rule_id.startswith("event.information."):
        source_name = rule_id.split(".", 3)[2]
        source_label = OFFICIAL_SOURCE_LABELS.get(source_name, "官方")
        if recovered:
            return f"{source_label} 官方信息风险已解除", details
        threshold = str(evidence.get("threshold", ""))
        phrase = threshold.removeprefix("official risk phrase: ").strip()
        if phrase:
            return f"{source_label} 官方公告提到：{phrase}", details
        return f"{source_label} 官方信息出现需要关注的变化", details

    if rule_id.startswith("information.attestation"):
        if recovered:
            return "BitGo 鉴证报告状态已恢复正常", details
        if "parse" in rule_id:
            return "BitGo 鉴证报告暂时无法完整解析", details
        if "field" in rule_id:
            return "BitGo 鉴证报告的关键信息发生变化", details
        return "BitGo 鉴证报告更新晚于预期", details

    if rule_id.startswith(("evm.", "event.evm.")):
        chain = _chain_label(rule_id, evidence)
        fact_type = str(evidence.get("fact_type", ""))
        fact = FACT_LABELS.get(fact_type)
        if fact is None:
            fact = "链上合约状态已恢复" if recovered else "检测到链上合约异常"
        summary = f"{chain} {fact}"
        for key, label in (
            ("account", "相关地址"),
            ("sender", "发起地址"),
            ("from_address", "转出地址"),
            ("to", "目标地址"),
            ("to_address", "转入地址"),
            ("amount", "数量"),
        ):
            value = evidence.get(key)
            if value is not None:
                details.append(f"{label}：{_format_number(value)}")
        return summary, details

    if recovered:
        return "此前发现的异常已恢复", details
    return "监控发现异常，请打开信息来源并人工确认", details


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


def _advice(
    level: RiskLevel, *, health_only: bool, por_age_only: bool = False
) -> str:
    if level is RiskLevel.GREEN:
        return "建议：继续观察一段时间。"
    if por_age_only:
        return "建议：请稍后查看官方储备页面是否恢复更新。"
    if health_only:
        return "建议：请检查监控服务和数据源是否正常。"
    return "建议：请打开信息来源并人工确认。"


def _event_lines(
    transitions: list[RiskTransition], *, timezone_name: str
) -> list[str]:
    lines: list[str] = []
    seen_lines: set[str] = set()
    sources: list[str] = []
    for transition in transitions:
        summary, details = _human_summary(transition)
        event_lines = [f"发生了什么：{summary}", *details]
        for line in event_lines:
            if line not in seen_lines:
                lines.append(line)
                seen_lines.add(line)
        sources.extend(_source_urls(transition.evidence))

    raw_time = transitions[0].evidence.get("data_time", transitions[0].changed_at)
    try:
        event_time = _display_time(raw_time, timezone_name)
    except (TypeError, ValueError):
        event_time = str(raw_time)
    time_label = (
        "恢复时间"
        if all(item.current is RiskLevel.GREEN for item in transitions)
        else "发现时间"
    )
    lines.append(f"{time_label}：{event_time}")

    unique_sources = list(dict.fromkeys(sources))
    if unique_sources:
        lines.append("信息来源：")
        lines.extend(unique_sources)
    return lines


def format_transitions(
    transitions: Iterable[RiskTransition],
    *,
    overall_level: RiskLevel | None = None,
    timezone_name: str = "Asia/Shanghai",
) -> str:
    items = list(transitions)
    if not items:
        raise ValueError("at least one transition is required")

    transition_level = max(item.current for item in items)
    health_only = all(is_monitoring_health_rule(item.rule_id) for item in items)
    por_age_only = all(item.rule_id == "por.age" for item in items)
    overall = overall_level if overall_level is not None else transition_level
    display_level = (
        transition_level if por_age_only else max(overall, transition_level)
    )
    recovered = (
        display_level is RiskLevel.GREEN
        and all(item.current is RiskLevel.GREEN for item in items)
        and any(item.previous is not RiskLevel.GREEN for item in items)
    )
    heading = _level_heading(
        display_level,
        recovered=recovered,
        health_only=health_only,
        por_age_only=por_age_only,
    )

    grouped: dict[str, list[RiskTransition]] = defaultdict(list)
    for index, item in enumerate(items):
        group_key = item.cause_id or f"rule:{index}:{item.rule_id}"
        grouped[group_key].append(item)

    lines = [heading]
    multiple_events = len(grouped) > 1
    for index, group_items in enumerate(grouped.values(), start=1):
        lines.append("")
        if multiple_events:
            lines.append(f"事件 {index}")
        lines.extend(_event_lines(group_items, timezone_name=timezone_name))
    lines.extend((
        "",
        _advice(
            display_level,
            health_only=health_only,
            por_age_only=por_age_only,
        ),
    ))
    return "\n".join(lines)
