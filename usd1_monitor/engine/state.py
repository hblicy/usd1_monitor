from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import datetime
from typing import Iterable

from usd1_monitor.models import RiskLevel, RiskTransition, RuleEvaluation
from usd1_monitor.engine.aggregate import (
    business_overall,
    health_overall,
    is_monitoring_health_rule,
)
from usd1_monitor.notifications.wechat import format_transitions, split_wechat_text
from usd1_monitor.storage import Storage


class StateEngine:
    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    async def apply(
        self,
        evaluations: Iterable[RuleEvaluation],
        now: datetime,
        *,
        enqueue_alerts: bool = True,
    ) -> list[RiskTransition]:
        connection = self._storage.connection
        async with self._storage.write_lock:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                if enqueue_alerts:
                    transitions = await self.apply_uncommitted(evaluations, now)
                else:
                    transitions = await self.apply_uncommitted(
                        evaluations, now, enqueue_alerts=False
                    )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        return transitions

    async def apply_uncommitted(
        self,
        evaluations: Iterable[RuleEvaluation],
        now: datetime,
        *,
        enqueue_alerts: bool = True,
    ) -> list[RiskTransition]:
        transitions: list[RiskTransition] = []
        for evaluation in evaluations:
            prior = await self._storage.get_risk_state(evaluation.rule_id)
            if prior is not None and prior.level is evaluation.level:
                continue

            previous = prior.level if prior is not None else RiskLevel.GREEN
            if prior is None or previous is RiskLevel.GREEN:
                first_triggered_at = now
            elif evaluation.level is RiskLevel.GREEN:
                first_triggered_at = now
            else:
                first_triggered_at = prior.first_triggered_at

            await self._storage.upsert_risk_state_uncommitted(
                evaluation.rule_id,
                evaluation.level,
                first_triggered_at,
                now,
            )
            if prior is None and evaluation.level is RiskLevel.GREEN:
                continue

            transitions.append(
                RiskTransition(
                    rule_id=evaluation.rule_id,
                    previous=previous,
                    current=evaluation.level,
                    changed_at=now,
                    first_triggered_at=first_triggered_at,
                    evidence=evaluation.evidence,
                    cause_id=evaluation.cause_id,
                )
            )

        if not enqueue_alerts:
            return transitions

        groups: dict[str, list[RiskTransition]] = defaultdict(list)
        for index, transition in enumerate(transitions):
            group_key = transition.cause_id or f"rule:{index}:{transition.rule_id}"
            groups[group_key].append(transition)
        states = await self._storage.list_risk_states()
        business_level = business_overall(
            states,
            now=now,
            event_active_seconds=self._storage.event_active_seconds,
        )
        health_level = health_overall(states)
        for group_key, group in groups.items():
            overall = (
                health_level
                if all(
                    is_monitoring_health_rule(item.rule_id) for item in group
                )
                else business_level
            )
            content = format_transitions(
                group,
                overall_level=overall,
                timezone_name=self._storage.timezone_name,
            )
            alert_key = f"{group_key}:{now.isoformat()}"
            chunks = split_wechat_text(content)
            for index, chunk in enumerate(chunks, start=1):
                chunk_key = (
                    alert_key
                    if len(chunks) == 1
                    else f"{alert_key}:part:{index:03d}"
                )
                await self._storage.insert_pending_alert_uncommitted(
                    chunk_key,
                    hashlib.sha256(chunk.encode("utf-8")).hexdigest(),
                    chunk,
                    now,
                )
        return transitions
