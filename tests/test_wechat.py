from datetime import UTC, datetime, timedelta

import pytest

import usd1_monitor.scheduler as scheduler
import usd1_monitor.notifications.wechat as wechat
from tests.fakes import FakeHttp
from usd1_monitor.engine.state import StateEngine
from usd1_monitor.models import RiskLevel, RiskTransition, RuleEvaluation
from usd1_monitor.notifications.wechat import (
    WeChatDeliveryError,
    WeChatNotifier,
    format_transitions,
)
from usd1_monitor.storage import Storage
from usd1_monitor.scheduler import _deliver_pending


NOW = datetime(2026, 9, 7, 4, 30, tzinfo=UTC)
WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=redacted"


def test_plain_text_helpers_render_chinese_status_time_and_sources() -> None:
    assert wechat._level_heading(RiskLevel.YELLOW, recovered=False) == "🟡 USD1 注意"
    assert wechat._level_heading(RiskLevel.GREEN, recovered=True) == "🟢 USD1 已恢复正常"
    assert wechat._display_time(NOW, "Asia/Shanghai") == (
        "2026-09-07 12:30:00（北京时间）"
    )
    assert wechat._source_urls(
        {"source_urls": ["https://one", "https://two"]}
    ) == [
        "https://one",
        "https://two",
    ]
    assert wechat._source_urls({}) == []
    assert wechat._display_time(NOW, "UTC") == "2026-09-07 04:30:00（UTC）"


@pytest.mark.asyncio
async def test_http_200_business_error_is_delivery_failure() -> None:
    http = FakeHttp()
    http.queue_json(WEBHOOK, {"errcode": 93000, "errmsg": "invalid webhook"}, "POST")
    notifier = WeChatNotifier(WEBHOOK, http)

    with pytest.raises(WeChatDeliveryError, match="93000"):
        await notifier.send_text("risk")


@pytest.mark.asyncio
async def test_successful_delivery_uses_enterprise_wechat_text_payload() -> None:
    http = FakeHttp()
    http.queue_json(WEBHOOK, {"errcode": 0, "errmsg": "ok"}, "POST")
    notifier = WeChatNotifier(WEBHOOK, http)

    await notifier.send_text("risk")

    assert http.calls == [
        ("POST", WEBHOOK, {"msgtype": "text", "text": {"content": "risk"}})
    ]


def transition(
    rule_id: str,
    previous: RiskLevel,
    current: RiskLevel,
    *,
    cause_id: str | None = None,
    evidence: dict[str, object] | None = None,
) -> RiskTransition:
    return RiskTransition(
        rule_id=rule_id,
        previous=previous,
        current=current,
        changed_at=NOW,
        first_triggered_at=NOW,
        evidence=evidence
        if evidence is not None
        else {
            "current": 0.996,
            "threshold": 0.997,
            "data_time": NOW.isoformat(),
            "source_url": "https://api.binance.com/api/v3/depth",
        },
        cause_id=cause_id,
    )


def test_alert_message_uses_plain_chinese_summary() -> None:
    content = format_transitions(
        [transition("market.price", RiskLevel.GREEN, RiskLevel.YELLOW)]
    )

    assert content.startswith("🟡 USD1 注意")
    assert "发生了什么：USD1 价格低于预警线" in content
    assert "当前价格：0.996" in content
    assert "预警价格：0.997" in content
    assert "发现时间：2026-09-07 12:30:00（北京时间）" in content
    assert "信息来源：\nhttps://api.binance.com/api/v3/depth" in content
    assert "建议：请打开信息来源并人工确认。" in content
    for hidden in (
        "market.price",
        "rule_id",
        "当前值=",
        "阈值=",
        "GREEN",
        "YELLOW",
        "##",
        "**",
        "- ",
        "](",
    ):
        assert hidden not in content


def test_custody_concentration_message_is_plain_and_explains_lower_bound() -> None:
    content = format_transitions(
        [
            transition(
                "custody.binance_concentration",
                RiskLevel.GREEN,
                RiskLevel.RED,
                evidence={
                    "share": 0.71,
                    "verified_balance": 3_000_000_000,
                    "data_time": NOW.isoformat(),
                    "source_urls": [
                        "https://www.binance.com/en/square/post/97671"
                    ],
                },
            )
        ],
        overall_level=RiskLevel.RED,
        timezone_name="Asia/Shanghai",
    )

    assert "已核验 Binance 地址至少占全网供应量 71%" in content
    assert "已核验余额：30 亿 USD1" in content
    assert "https://www.binance.com/en/square/post/97671" in content
    for hidden in (
        "custody.binance_concentration",
        "rule_id",
        "verified_balance",
        "source_urls",
        "**",
        "- ",
    ):
        assert hidden not in content


@pytest.mark.parametrize(
    ("rule_id", "evidence", "expected"),
    (
        (
            "custody.binance_flow_24h",
            {
                "value": 60_000_000,
                "chain": "ethereum",
                "label": "Binance 已核验地址组",
                "data_time": NOW.isoformat(),
            },
            "Ethereum Binance 已核验地址组 24 小时净流入 6000 万 USD1",
        ),
        (
            "custody.address_outflow_1h",
            {
                "values": {"bsc:0x1234": 120_000_000},
                "labels": {"bsc:0x1234": "Binance Hot Wallet"},
                "data_time": NOW.isoformat(),
            },
            "BNB Chain Binance Hot Wallet 1 小时净流出 1.2 亿 USD1",
        ),
        (
            "custody.binance_flow_24h",
            {
                "value": -55_000_000,
                "chain": "solana",
                "label": "Binance 2",
                "data_time": NOW.isoformat(),
            },
            "Solana Binance 2 24 小时余额减少 5500 万 USD1",
        ),
    ),
)
def test_custody_flow_messages_explain_chain_label_amount_and_window(
    rule_id: str, evidence: dict[str, object], expected: str
) -> None:
    content = format_transitions(
        [transition(rule_id, RiskLevel.GREEN, RiskLevel.YELLOW, evidence=evidence)]
    )

    assert expected in content
    for hidden in (rule_id, "rule_id", "values", "labels", "**", "- "):
        assert hidden not in content


def test_business_alert_without_source_points_to_dashboard() -> None:
    content = format_transitions(
        [
            transition(
                "custody.binance_flow_24h",
                RiskLevel.GREEN,
                RiskLevel.YELLOW,
                evidence={
                    "value": 60_000_000,
                    "chains": ["ethereum"],
                    "labels": ["Binance ETH"],
                    "data_time": NOW.isoformat(),
                },
            )
        ]
    )

    assert "信息来源：" not in content
    assert "建议：请打开监控面板查看详情。" in content
    assert "请打开信息来源" not in content


def test_custody_flow_messages_degrade_safely_without_context() -> None:
    entity_message = format_transitions(
        [
            transition(
                "custody.binance_flow_24h",
                RiskLevel.GREEN,
                RiskLevel.YELLOW,
                evidence={
                    "value": -60_000_000,
                    "data_time": NOW.isoformat(),
                },
            )
        ]
    )
    address_message = format_transitions(
        [
            transition(
                "custody.address_outflow_1h",
                RiskLevel.GREEN,
                RiskLevel.YELLOW,
                evidence={
                    "values": {"ethereum:0x1234": 120_000_000},
                    "data_time": NOW.isoformat(),
                },
            )
        ]
    )

    assert "相关链上 已核验 Binance 地址组 24 小时净流出 6000 万 USD1" in entity_message
    assert "Ethereum 已核验地址 0x1234 1 小时净流出 1.2 亿 USD1" in address_message


@pytest.mark.parametrize(
    ("rule_id", "expected"),
    (
        ("custody.binance_concentration", "集中度已回到预警线内"),
        ("custody.binance_flow_24h", "大额资金变化已结束"),
        ("custody.address_outflow_1h", "大额资金变化已结束"),
        ("redemption.channel", "官方赎回限制已解除"),
    ),
)
def test_core_asset_recovery_messages_are_specific(
    rule_id: str, expected: str
) -> None:
    content = format_transitions(
        [
            transition(
                rule_id,
                RiskLevel.YELLOW,
                RiskLevel.GREEN,
                evidence={"data_time": NOW.isoformat()},
            )
        ]
    )

    assert f"发生了什么：{expected}" in content
    assert rule_id not in content


def test_redemption_message_uses_chinese_summary_and_official_source() -> None:
    content = format_transitions(
        [
            transition(
                "redemption.channel",
                RiskLevel.GREEN,
                RiskLevel.RED,
                evidence={
                    "summary": "USD1 官方赎回已暂停或不可用",
                    "data_time": NOW.isoformat(),
                    "source_url": "https://www.bitgo.com/usd1/",
                },
            )
        ]
    )

    assert "发生了什么：USD1 官方赎回已暂停或不可用" in content
    assert "信息来源：\nhttps://www.bitgo.com/usd1/" in content
    assert "redemption.channel" not in content


def test_generic_bitgo_incident_says_not_confirmed() -> None:
    content = format_transitions(
        [
            transition(
                "redemption.channel",
                RiskLevel.GREEN,
                RiskLevel.YELLOW,
                evidence={
                    "summary": "BitGo Stablecoins 服务异常，可能影响 USD1，尚未确认",
                    "data_time": NOW.isoformat(),
                    "source_url": "https://status.bitgo.com/",
                },
            )
        ],
        overall_level=RiskLevel.YELLOW,
        timezone_name="Asia/Shanghai",
    )

    assert "可能影响 USD1，尚未确认" in content
    assert "https://status.bitgo.com/" in content


def test_alert_message_renders_all_source_urls() -> None:
    item = transition("market.price", RiskLevel.GREEN, RiskLevel.YELLOW)
    item.evidence.pop("source_url")
    item.evidence["source_urls"] = [
        "https://api.binance.com/api/v3/depth?symbol=USD1USDC&limit=1000",
        "https://api.binance.com/api/v3/depth?symbol=USD1USDT&limit=1000",
    ]

    content = format_transitions([item])

    assert "symbol=USD1USDC" in content
    assert "symbol=USD1USDT" in content
    assert "来源=未提供" not in content


def test_alert_message_contains_severe_and_exit_capacity_details() -> None:
    item = transition("market.price.severe", RiskLevel.GREEN, RiskLevel.RED)
    item.evidence.update(
        {
            "severe_threshold": 0.99,
            "severe_duration_seconds": 3600,
            "exit_capacity": {
                "USD1USDT": {
                    "5000000": {
                        "terminal_price": 0.997,
                        "fully_fillable": True,
                    }
                }
            },
        }
    )

    content = format_transitions([item])

    assert "发生了什么：USD1 价格持续严重偏离 1 美元" in content
    assert "危险价格：0.99" in content
    assert "持续条件：60 分钟" in content
    assert "exit_capacity" not in content
    assert "severe_threshold" not in content


def test_alert_times_default_to_asia_shanghai() -> None:
    content = format_transitions(
        [transition("market.price", RiskLevel.GREEN, RiskLevel.YELLOW)]
    )

    assert "2026-09-07 12:30:00（北京时间）" in content


def test_recovery_message_names_previous_level() -> None:
    content = format_transitions(
        [transition("market.price", RiskLevel.RED, RiskLevel.GREEN)]
    )

    assert content.startswith("🟢 USD1 已恢复正常")
    assert "发生了什么：USD1 价格已恢复正常" in content
    assert "RED" not in content


def test_explicit_green_overall_is_not_replaced_by_event_level() -> None:
    content = format_transitions(
        [transition("event.information.notice", RiskLevel.GREEN, RiskLevel.YELLOW)],
        overall_level=RiskLevel.GREEN,
    )

    assert content.startswith("🟡 USD1 注意")
    assert "GREEN" not in content
    assert "YELLOW" not in content


def test_same_cause_is_rendered_once_with_two_evidence_lines() -> None:
    content = format_transitions(
        [
            transition("market.price", RiskLevel.GREEN, RiskLevel.RED, cause_id="depeg"),
            transition("market.liquidity", RiskLevel.GREEN, RiskLevel.RED, cause_id="depeg"),
        ]
    )

    assert "depeg" not in content
    assert content.count("发生了什么：") == 2


def test_evm_alert_uses_human_summary_and_hides_internal_fields() -> None:
    event = RiskTransition(
        rule_id="evm.event.ethereum.0xtx:-1",
        previous=RiskLevel.GREEN,
        current=RiskLevel.YELLOW,
        changed_at=NOW,
        first_triggered_at=NOW,
        evidence={
            "fact_type": "PRIVILEGED_UNKNOWN_CALL",
            "chain": "ethereum",
            "event_key": "0xtx:-1",
            "sender": "0xsender",
            "to": "0xtarget",
            "selector": "0x12345678",
        },
        cause_id="0xtx:-1",
    )

    content = format_transitions([event])

    assert "发生了什么：Ethereum 检测到未知的高权限合约调用" in content
    assert "发起地址：0xsender" in content
    assert "目标地址：0xtarget" in content
    for hidden in ("fact_type", "event_key", "selector", "0xtx:-1", "0x12345678"):
        assert hidden not in content


def test_proxy_admin_owner_alert_uses_human_summary() -> None:
    event = RiskTransition(
        rule_id="evm.event.ethereum.snapshot:101:evm.admin_owner",
        previous=RiskLevel.GREEN,
        current=RiskLevel.RED,
        changed_at=NOW,
        first_triggered_at=NOW,
        evidence={
            "fact_type": "ADMIN_OWNER_CHANGED",
            "chain": "ethereum",
            "previous": "0x" + "11" * 20,
            "current": "0x" + "22" * 20,
            "block": 101,
        },
    )

    content = format_transitions([event])

    assert "发生了什么：Ethereum USD1 代理管理员控制人发生变化" in content
    assert "ADMIN_OWNER_CHANGED" not in content


def test_health_alert_is_distinct_from_business_risk() -> None:
    health = RiskTransition(
        rule_id="health.por",
        previous=RiskLevel.GREEN,
        current=RiskLevel.YELLOW,
        changed_at=NOW,
        first_triggered_at=NOW,
        evidence={"current": 3, "threshold": 3, "last_error": "RPC timeout"},
    )

    content = format_transitions([health])

    assert content.startswith("🟡 USD1 监控异常")
    assert "发生了什么：储备证明数据连续 3 次未能获取" in content
    assert "建议：请检查监控服务和数据源是否正常。" in content
    assert "RPC timeout" not in content
    assert "health.por" not in content


def test_single_health_recovery_does_not_hide_other_health_failure() -> None:
    content = format_transitions(
        [transition("health.por", RiskLevel.YELLOW, RiskLevel.GREEN)],
        overall_level=RiskLevel.RED,
    )

    assert content.startswith("🔴 USD1 监控异常")
    assert "🟢 USD1 监控已恢复" not in content


def test_por_age_red_alert_explains_update_delay() -> None:
    item = transition("por.age", RiskLevel.GREEN, RiskLevel.RED)
    item.evidence.update({
        "current": 128.58028327 * 60,
        "source_url": "https://www.bitgo.com/resources/proof-of-reserves",
    })

    content = format_transitions([item])

    assert content.startswith("🔴 USD1 储备数据更新延迟")
    assert (
        "发生了什么：官方储备数据已有约 129 分钟未更新\n"
        "说明：这不代表储备不足，只表示目前无法获得最新储备信息。"
    ) in content
    assert "发现时间：2026-09-07 12:30:00（北京时间）" in content
    assert "https://www.bitgo.com/resources/proof-of-reserves" in content
    assert "建议：请稍后查看官方储备页面是否恢复更新。" in content
    assert "USD1 危险" not in content
    assert "128.58028327" not in content


def test_por_age_green_recovery_uses_dedicated_message() -> None:
    item = transition("por.age", RiskLevel.RED, RiskLevel.GREEN)
    item.evidence["source_url"] = (
        "https://www.bitgo.com/resources/proof-of-reserves"
    )

    content = format_transitions([item])

    assert content.startswith("🟢 USD1 储备数据已恢复更新")
    assert "发生了什么：USD1 储备数据已恢复更新" in content
    assert "恢复时间：2026-09-07 12:30:00（北京时间）" in content
    assert "https://www.bitgo.com/resources/proof-of-reserves" in content
    assert "建议：继续观察一段时间。" in content


def test_bridge_overissue_message_is_plain_chinese() -> None:
    message = format_transitions(
        [
            RiskTransition(
                rule_id="supply.bridge_reconciliation",
                previous=RiskLevel.GREEN,
                current=RiskLevel.YELLOW,
                changed_at=NOW,
                first_triggered_at=NOW,
                evidence={
                    "direction": "overissued",
                    "issued": 1_250_000_000,
                    "locked": 1_249_650_000,
                    "difference": 350_000,
                    "difference_percent": 0.028,
                    "data_time": NOW.isoformat(),
                },
            )
        ]
    )

    assert "跨链发行量比桥池锁仓量多 35 万 USD1" in message
    assert "supply.bridge_reconciliation" not in message
    assert "threshold" not in message


@pytest.mark.parametrize(
    ("rule_id", "evidence", "expected"),
    (
        ("market.liquidity", {}, "USD1 市场流动性不足"),
        (
            "supply.estimated_coverage",
            {"current": 98.4},
            "USD1 估算储备覆盖率异常",
        ),
        (
            "event.information.binance.notice.hash",
            {"threshold": "official risk phrase: USD1 withdrawals are suspended"},
            "Binance 官方公告提到：USD1 withdrawals are suspended",
        ),
        (
            "event.information.bitgo.attestation_fields.2026-07:report",
            {"current": ["auditor"]},
            "BitGo 鉴证报告的关键信息发生变化",
        ),
        (
            "health.supply_bsc",
            {"current": 3},
            "BNB Chain 供应量数据连续 3 次未能获取",
        ),
    ),
)
def test_existing_rule_types_have_human_summary(
    rule_id: str, evidence: dict[str, object], expected: str
) -> None:
    item = transition(rule_id, RiskLevel.GREEN, RiskLevel.YELLOW)
    item.evidence.update(evidence)

    assert expected in format_transitions([item])


def test_unknown_rule_uses_generic_summary_without_internal_id() -> None:
    content = format_transitions(
        [transition("future.internal.rule", RiskLevel.GREEN, RiskLevel.YELLOW)]
    )

    assert "发生了什么：监控发现异常，请打开信息来源并人工确认" in content
    assert "future.internal.rule" not in content


@pytest.mark.asyncio
async def test_failed_pending_delivery_is_attempted_at_most_twice(storage) -> None:
    await StateEngine(storage).apply(
        [RuleEvaluation("market.price", RiskLevel.YELLOW, {"current": 0.996})],
        NOW,
    )
    pending = await storage.pending_alerts()
    assert len(pending) == 1

    assert await storage.claim_alert_delivery(pending[0].id) is True
    await storage.record_delivery_result(pending[0].id, NOW, error="timeout")
    retry = await storage.pending_alerts()
    assert len(retry) == 1
    assert retry[0].attempts == 1

    assert await storage.claim_alert_delivery(retry[0].id) is True
    await storage.record_delivery_result(retry[0].id, NOW, error="timeout again")
    assert await storage.pending_alerts() == []
    failed = await storage.failed_alerts()
    assert len(failed) == 1
    assert failed[0].attempts == 2


@pytest.mark.asyncio
async def test_dead_letter_keeps_notification_health_yellow_after_later_success(
    storage,
) -> None:
    class FailingNotifier:
        async def send_text(self, content: str) -> None:
            raise TimeoutError("offline")

    class SuccessfulNotifier:
        async def send_text(self, content: str) -> None:
            return None

    await StateEngine(storage).apply(
        [RuleEvaluation("market.price", RiskLevel.YELLOW)], NOW
    )
    await _deliver_pending(storage, FailingNotifier())
    await _deliver_pending(storage, FailingNotifier())

    failed_state = await storage.get_risk_state("health.notification_wechat")
    assert failed_state is not None and failed_state.level is RiskLevel.YELLOW

    await StateEngine(storage).apply(
        [RuleEvaluation("market.price", RiskLevel.RED)],
        NOW + timedelta(seconds=1),
    )
    await _deliver_pending(storage, SuccessfulNotifier())

    current_state = await storage.get_risk_state("health.notification_wechat")
    assert current_state is not None and current_state.level is RiskLevel.YELLOW
    assert len(await storage.failed_alerts()) == 1


@pytest.mark.asyncio
async def test_recovery_alert_uses_complete_persisted_overall_state(storage) -> None:
    await storage.set_risk_state("evm.ethereum.pause", RiskLevel.RED, NOW, NOW)
    await storage.set_risk_state("market.price", RiskLevel.RED, NOW, NOW)

    await StateEngine(storage).apply(
        [RuleEvaluation("market.price", RiskLevel.GREEN)], NOW
    )

    pending = await storage.pending_alerts()
    assert len(pending) == 1
    assert pending[0].content.startswith("🔴 USD1 危险")
    assert "🟢 USD1 已恢复正常" not in pending[0].content


@pytest.mark.asyncio
async def test_health_transitions_are_persisted_without_wechat_alerts(storage) -> None:
    transitions = await StateEngine(storage).apply(
        [
            RuleEvaluation("health.por", RiskLevel.RED),
            RuleEvaluation("por.age", RiskLevel.YELLOW),
        ],
        NOW,
    )

    assert [(item.rule_id, item.current) for item in transitions] == [
        ("health.por", RiskLevel.RED),
        ("por.age", RiskLevel.YELLOW),
    ]
    assert await storage.pending_alerts() == []
    por_state = await storage.get_risk_state("health.por")
    age_state = await storage.get_risk_state("por.age")
    assert por_state is not None and por_state.level is RiskLevel.RED
    assert age_state is not None and age_state.level is RiskLevel.YELLOW


@pytest.mark.asyncio
async def test_mixed_transitions_only_enqueue_business_alerts(storage) -> None:
    await StateEngine(storage).apply(
        [
            RuleEvaluation("health.por", RiskLevel.RED),
            RuleEvaluation("market.price", RiskLevel.YELLOW),
        ],
        NOW,
    )

    pending = await storage.pending_alerts()
    assert len(pending) == 1
    assert pending[0].content.startswith("🟡 USD1 注意")
    assert "监控异常" not in pending[0].content
    health_state = await storage.get_risk_state("health.por")
    assert health_state is not None and health_state.level is RiskLevel.RED


@pytest.mark.asyncio
async def test_health_recovery_is_persisted_without_wechat_alert(storage) -> None:
    await storage.set_risk_state("health.por", RiskLevel.RED, NOW, NOW)

    transitions = await StateEngine(storage).apply(
        [RuleEvaluation("health.por", RiskLevel.GREEN)], NOW
    )

    assert [(item.rule_id, item.current) for item in transitions] == [
        ("health.por", RiskLevel.GREEN),
    ]
    assert await storage.pending_alerts() == []
    health_state = await storage.get_risk_state("health.por")
    assert health_state is not None and health_state.level is RiskLevel.GREEN


@pytest.mark.asyncio
async def test_por_age_transition_is_persisted_without_wechat_alert(
    storage,
) -> None:
    await storage.set_risk_state("market.price", RiskLevel.GREEN, NOW, NOW)
    await storage.set_risk_state("health.por", RiskLevel.RED, NOW, NOW)

    await StateEngine(storage).apply(
        [RuleEvaluation("por.age", RiskLevel.YELLOW)], NOW
    )

    assert await storage.pending_alerts() == []
    age_state = await storage.get_risk_state("por.age")
    assert age_state is not None and age_state.level is RiskLevel.YELLOW


@pytest.mark.asyncio
async def test_alert_times_use_configured_timezone(tmp_path) -> None:
    storage = Storage(tmp_path / "timezone.db", timezone_name="UTC")
    await storage.open()
    try:
        await StateEngine(storage).apply(
            [RuleEvaluation(
                "market.price",
                RiskLevel.YELLOW,
                {"current": 0.996, "data_time": NOW.isoformat()},
            )],
            NOW,
        )
        pending = await storage.pending_alerts()
    finally:
        await storage.close()

    assert "2026-09-07 04:30:00（UTC）" in pending[0].content


@pytest.mark.asyncio
async def test_state_engine_splits_alerts_at_wechat_utf8_limit(storage) -> None:
    evaluation = RuleEvaluation(
        "market.price",
        RiskLevel.RED,
        {"source_url": "https://example.com/" + "a" * 5000},
        cause_id="oversized-alert",
    )
    expected = format_transitions(
        [
            RiskTransition(
                rule_id=evaluation.rule_id,
                previous=RiskLevel.GREEN,
                current=evaluation.level,
                changed_at=NOW,
                first_triggered_at=NOW,
                evidence=evaluation.evidence,
                cause_id=evaluation.cause_id,
            )
        ],
        overall_level=RiskLevel.RED,
    )

    await StateEngine(storage).apply([evaluation], NOW)

    pending = await storage.pending_alerts()
    assert len(pending) > 1
    assert all(len(item.content.encode("utf-8")) <= 2048 for item in pending)
    assert "".join(item.content for item in pending) == expected


@pytest.mark.asyncio
async def test_delivery_rate_limit_is_shared_across_pending_drains(storage) -> None:
    limiter_type = getattr(scheduler, "DeliveryRateLimiter", None)
    assert limiter_type is not None
    clock = [NOW]
    limiter = limiter_type(
        storage,
        max_messages=2,
        window_seconds=60,
        clock=lambda: clock[0],
    )

    class RecordingNotifier:
        messages: list[str] = []

        async def send_text(self, content: str) -> None:
            self.messages.append(content)

    notifier = RecordingNotifier()
    await StateEngine(storage).apply(
        [
            RuleEvaluation(
                f"market.test.{index}",
                RiskLevel.YELLOW,
                cause_id=f"cause-{index}",
            )
            for index in range(3)
        ],
        NOW,
    )

    await _deliver_pending(storage, notifier, rate_limiter=limiter)
    await _deliver_pending(storage, notifier, rate_limiter=limiter)

    assert len(notifier.messages) == 2
    assert len(await storage.pending_alerts()) == 1

    clock[0] += timedelta(seconds=60)
    await _deliver_pending(storage, notifier, rate_limiter=limiter)

    assert len(notifier.messages) == 3
    assert await storage.pending_alerts() == []


@pytest.mark.asyncio
async def test_cancelled_pending_alert_does_not_consume_delivery_slot(
    storage, monkeypatch
) -> None:
    limiter = scheduler.DeliveryRateLimiter(
        storage,
        max_messages=1,
        window_seconds=60,
        clock=lambda: NOW,
    )

    class RecordingNotifier:
        messages: list[str] = []

        async def send_text(self, content: str) -> None:
            self.messages.append(content)

    notifier = RecordingNotifier()
    await StateEngine(storage).apply(
        [
            RuleEvaluation(
                "evm.bsc.paused",
                RiskLevel.RED,
                cause_id="snapshot:bsc:100:paused",
            )
        ],
        NOW,
    )
    original_pending_alerts = storage.pending_alerts
    cancel_before_claim = True

    async def pending_then_cancel(*, max_attempts=2):
        nonlocal cancel_before_claim
        pending = await original_pending_alerts(max_attempts=max_attempts)
        if cancel_before_claim:
            cancel_before_claim = False
            async with storage.write_lock:
                await storage.connection.execute("BEGIN IMMEDIATE")
                await storage.cancel_pending_alerts_for_cause_uncommitted(
                    "snapshot:bsc:100:paused"
                )
                await storage.connection.commit()
        return pending

    monkeypatch.setattr(storage, "pending_alerts", pending_then_cancel)
    await _deliver_pending(storage, notifier, rate_limiter=limiter)
    assert notifier.messages == []

    monkeypatch.setattr(storage, "pending_alerts", original_pending_alerts)
    await StateEngine(storage).apply(
        [
            RuleEvaluation(
                "market.current",
                RiskLevel.YELLOW,
                cause_id="current-risk",
            )
        ],
        NOW,
    )
    await _deliver_pending(storage, notifier, rate_limiter=limiter)

    assert len(notifier.messages) == 1
    assert "监控发现异常" in notifier.messages[0]
    assert "market.current" not in notifier.messages[0]


@pytest.mark.asyncio
async def test_delivery_failure_defers_retry_for_rate_window(storage) -> None:
    limiter_type = getattr(scheduler, "DeliveryRateLimiter", None)
    assert limiter_type is not None
    clock = [NOW]
    limiter = limiter_type(
        storage,
        max_messages=20,
        window_seconds=60,
        clock=lambda: clock[0],
    )

    class FailOnceNotifier:
        attempts = 0

        async def send_text(self, content: str) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise WeChatDeliveryError("rate limited")

    notifier = FailOnceNotifier()
    await StateEngine(storage).apply(
        [RuleEvaluation("market.price", RiskLevel.YELLOW)], NOW
    )

    await _deliver_pending(storage, notifier, rate_limiter=limiter)
    await _deliver_pending(storage, notifier, rate_limiter=limiter)

    assert notifier.attempts == 1
    assert len(await storage.pending_alerts()) == 1

    clock[0] += timedelta(seconds=60)
    await _deliver_pending(storage, notifier, rate_limiter=limiter)

    assert notifier.attempts == 2
    assert await storage.pending_alerts() == []


@pytest.mark.asyncio
async def test_delivery_rate_window_survives_process_restart(tmp_path) -> None:
    clock = [NOW]
    db_path = tmp_path / "restart-rate-limit.db"
    storage = Storage(db_path)
    await storage.open()
    first = scheduler.DeliveryRateLimiter(
        storage,
        max_messages=2,
        window_seconds=60,
        clock=lambda: clock[0],
    )

    assert await first.try_acquire() is True
    assert await first.try_acquire() is True
    await storage.close()

    storage = Storage(db_path)
    await storage.open()
    restarted = scheduler.DeliveryRateLimiter(
        storage,
        max_messages=2,
        window_seconds=60,
        clock=lambda: clock[0],
    )
    assert await restarted.try_acquire() is False

    clock[0] += timedelta(seconds=60)
    assert await restarted.try_acquire() is True
    await storage.close()


@pytest.mark.asyncio
async def test_delivery_failure_cooldown_survives_limiter_reconstruction(
    tmp_path,
) -> None:
    clock = [NOW]
    db_path = tmp_path / "restart-cooldown.db"
    storage = Storage(db_path)
    await storage.open()

    class FailOnceNotifier:
        attempts = 0

        async def send_text(self, content: str) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise WeChatDeliveryError("rate limited")

    notifier = FailOnceNotifier()
    await StateEngine(storage).apply(
        [RuleEvaluation("market.restart", RiskLevel.YELLOW)], NOW
    )
    first = scheduler.DeliveryRateLimiter(
        storage,
        max_messages=20,
        window_seconds=60,
        clock=lambda: clock[0],
    )

    await _deliver_pending(storage, notifier, rate_limiter=first)
    await storage.close()

    storage = Storage(db_path)
    await storage.open()
    restarted = scheduler.DeliveryRateLimiter(
        storage,
        max_messages=20,
        window_seconds=60,
        clock=lambda: clock[0],
    )
    await _deliver_pending(storage, notifier, rate_limiter=restarted)
    assert notifier.attempts == 1

    clock[0] += timedelta(seconds=60)
    await _deliver_pending(storage, notifier, rate_limiter=restarted)
    assert notifier.attempts == 2
    assert await storage.pending_alerts() == []
    await storage.close()
