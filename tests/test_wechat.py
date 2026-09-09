from datetime import UTC, datetime, timedelta

import pytest

import usd1_monitor.scheduler as scheduler
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
) -> RiskTransition:
    return RiskTransition(
        rule_id=rule_id,
        previous=previous,
        current=current,
        changed_at=NOW,
        first_triggered_at=NOW,
        evidence={
            "current": 0.996,
            "threshold": 0.997,
            "data_time": NOW.isoformat(),
            "source_url": "https://api.binance.com/api/v3/depth",
        },
        cause_id=cause_id,
    )


def test_alert_message_contains_required_evidence() -> None:
    content = format_transitions(
        [transition("market.price", RiskLevel.GREEN, RiskLevel.YELLOW)]
    )

    assert "YELLOW" in content
    assert "market.price" in content
    assert "0.996" in content
    assert "0.997" in content
    assert "2026-09-07T12:30:00+08:00" in content
    assert "https://api.binance.com/api/v3/depth" in content


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

    assert "severe_threshold=0.99" in content
    assert "severe_duration_seconds=3600" in content
    assert "exit_capacity=" in content


def test_alert_times_default_to_asia_shanghai() -> None:
    content = format_transitions(
        [transition("market.price", RiskLevel.GREEN, RiskLevel.YELLOW)]
    )

    assert "2026-09-07T12:30:00+08:00" in content


def test_recovery_message_names_previous_level() -> None:
    content = format_transitions(
        [transition("market.price", RiskLevel.RED, RiskLevel.GREEN)]
    )

    assert "恢复" in content
    assert "RED" in content


def test_explicit_green_overall_is_not_replaced_by_event_level() -> None:
    content = format_transitions(
        [transition("event.information.notice", RiskLevel.GREEN, RiskLevel.YELLOW)],
        overall_level=RiskLevel.GREEN,
    )

    assert content.startswith("USD1 风险事件：YELLOW")
    assert "当前持续状态：GREEN" in content


def test_same_cause_is_rendered_once_with_two_evidence_lines() -> None:
    content = format_transitions(
        [
            transition("market.price", RiskLevel.GREEN, RiskLevel.RED, cause_id="depeg"),
            transition("market.liquidity", RiskLevel.GREEN, RiskLevel.RED, cause_id="depeg"),
        ]
    )

    assert content.count("原因 depeg") == 1
    assert content.count("当前值=0.996") == 2


def test_evm_alert_includes_transaction_evidence_fields() -> None:
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

    for expected in (
        "fact_type=PRIVILEGED_UNKNOWN_CALL",
        "chain=ethereum",
        "event_key=0xtx:-1",
        "sender=0xsender",
        "to=0xtarget",
        "selector=0x12345678",
    ):
        assert expected in content
    assert "unknown" not in content


def test_health_alert_includes_last_error() -> None:
    health = RiskTransition(
        rule_id="health.por",
        previous=RiskLevel.GREEN,
        current=RiskLevel.YELLOW,
        changed_at=NOW,
        first_triggered_at=NOW,
        evidence={"current": 3, "threshold": 3, "last_error": "RPC timeout"},
    )

    content = format_transitions([health])

    assert "last_error=RPC timeout" in content


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
    assert "USD1 风险状态：RED" in pending[0].content


@pytest.mark.asyncio
async def test_health_alert_uses_monitor_health_not_business_overall(storage) -> None:
    await StateEngine(storage).apply(
        [RuleEvaluation("health.por", RiskLevel.RED)], NOW
    )

    pending = await storage.pending_alerts()

    assert pending[0].content.startswith("USD1 监控健康：RED")
    assert "当前持续状态：GREEN" not in pending[0].content


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

    assert "2026-09-07T04:30:00+00:00" in pending[0].content


@pytest.mark.asyncio
async def test_state_engine_splits_alerts_at_wechat_utf8_limit(storage) -> None:
    evaluation = RuleEvaluation(
        "health.official_wlfi",
        RiskLevel.RED,
        {"last_error": "采集失败" * 800},
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
    assert "market.current" in notifier.messages[0]


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
