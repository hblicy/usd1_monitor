from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from usd1_monitor.models import RiskLevel, RuleEvaluation


@dataclass(frozen=True)
class EvmFact:
    chain: str
    fact_type: str
    data: dict[str, Any]
    event_key: str
    cause_id: str | None = None


RED_IMMUTABLE_FACTS = {
    "IMPLEMENTATION_CHANGED",
    "ADMIN_CHANGED",
    "ADMIN_OWNER_CHANGED",
    "CODE_HASH_CHANGED",
}

EXPLORER_BASE_URLS = {
    "ethereum": "https://etherscan.io",
    "bsc": "https://bscscan.com",
}


def _source_url(fact: EvmFact) -> str | None:
    base = EXPLORER_BASE_URLS.get(fact.chain)
    if base is None:
        return None
    if fact.event_key.startswith("0x"):
        return f"{base}/tx/{fact.event_key.split(':', 1)[0]}"
    block = fact.data.get("block")
    if isinstance(block, int) and block >= 0:
        return f"{base}/block/{block}"
    return None


def evaluate_evm_fact(
    fact: EvmFact, watched_addresses: set[str]
) -> RuleEvaluation:
    watched = {address.lower() for address in watched_addresses}
    fact_type = fact.fact_type.upper()
    cause_id = fact.cause_id or fact.event_key
    evidence = {
        "chain": fact.chain,
        "fact_type": fact_type,
        "event_key": fact.event_key,
        **fact.data,
    }
    source_url = _source_url(fact)
    if source_url is not None:
        evidence["source_url"] = source_url

    if fact_type == "PAUSED":
        return RuleEvaluation(
            f"evm.{fact.chain}.paused", RiskLevel.RED, evidence, cause_id
        )
    if fact_type == "UNPAUSED":
        return RuleEvaluation(
            f"evm.{fact.chain}.paused", RiskLevel.GREEN, evidence, cause_id
        )
    if fact_type in {"FREEZE", "UNFREEZE"}:
        account = str(fact.data.get("account", "")).lower()
        rule_id = f"evm.{fact.chain}.freeze.{account}"
        if fact_type == "UNFREEZE":
            return RuleEvaluation(rule_id, RiskLevel.GREEN, evidence, cause_id)
        level = RiskLevel.RED if account in watched else RiskLevel.YELLOW
        return RuleEvaluation(rule_id, level, evidence, cause_id)
    if fact_type in RED_IMMUTABLE_FACTS:
        return RuleEvaluation(
            f"evm.event.{fact.chain}.{fact.event_key}",
            RiskLevel.RED,
            evidence,
            cause_id,
        )
    if fact_type == "PRIVILEGED_UNKNOWN_CALL":
        level = (
            RiskLevel.RED
            if bool(fact.data.get("third_party_asset_move"))
            else RiskLevel.YELLOW
        )
        return RuleEvaluation(
            f"evm.event.{fact.chain}.{fact.event_key}",
            level,
            evidence,
            cause_id,
        )
    if fact_type in {"OWNER_CHANGED", "FREEZE_ORDINARY"}:
        return RuleEvaluation(
            f"evm.event.{fact.chain}.{fact.event_key}",
            RiskLevel.YELLOW,
            evidence,
            cause_id,
        )
    if fact_type in {"MINT", "BURN"}:
        amount = float(fact.data.get("amount", 0))
        level = RiskLevel.YELLOW if amount >= 10_000_000 else RiskLevel.GREEN
        return RuleEvaluation(
            f"event.evm.{fact.chain}.{fact.event_key}",
            level,
            evidence,
            cause_id,
        )
    return RuleEvaluation(
        f"evm.event.{fact.chain}.{fact.event_key}",
        RiskLevel.GREEN,
        evidence,
        cause_id,
    )
