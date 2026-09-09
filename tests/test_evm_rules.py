from usd1_monitor.engine.evm_rules import EvmFact, evaluate_evm_fact
from usd1_monitor.models import RiskLevel


def test_implementation_change_is_red() -> None:
    fact = EvmFact("ethereum", "IMPLEMENTATION_CHANGED", {}, "0xtx")

    assert evaluate_evm_fact(fact, watched_addresses=set()).level is RiskLevel.RED


def test_watched_freeze_is_red_and_other_freeze_is_yellow() -> None:
    watched = {"0x" + "11" * 20}
    critical = EvmFact(
        "bsc", "FREEZE", {"account": "0x" + "11" * 20}, "0xa"
    )
    ordinary = EvmFact(
        "bsc", "FREEZE", {"account": "0x" + "22" * 20}, "0xb"
    )

    assert evaluate_evm_fact(critical, watched).level is RiskLevel.RED
    assert evaluate_evm_fact(ordinary, watched).level is RiskLevel.YELLOW


def test_unknown_privileged_asset_move_is_red() -> None:
    fact = EvmFact(
        "ethereum",
        "PRIVILEGED_UNKNOWN_CALL",
        {"third_party_asset_move": True},
        "0xc",
    )

    assert evaluate_evm_fact(fact, set()).level is RiskLevel.RED


def test_unpause_and_unfreeze_recover_matching_state_keys() -> None:
    account = "0x" + "11" * 20
    unpause = evaluate_evm_fact(
        EvmFact("ethereum", "UNPAUSED", {}, "0x1"), set()
    )
    unfreeze = evaluate_evm_fact(
        EvmFact("ethereum", "UNFREEZE", {"account": account}, "0x2"), set()
    )

    assert unpause.rule_id == "evm.ethereum.paused"
    assert unpause.level is RiskLevel.GREEN
    assert unfreeze.rule_id == f"evm.ethereum.freeze.{account.lower()}"
    assert unfreeze.level is RiskLevel.GREEN


def test_large_mint_is_yellow() -> None:
    result = evaluate_evm_fact(
        EvmFact("bsc", "MINT", {"amount": 10_000_000}, "0xmint:1"), set()
    )

    assert result.level is RiskLevel.YELLOW
    assert "0xmint:1" in result.rule_id


def test_event_evidence_links_to_chain_explorer_transaction() -> None:
    result = evaluate_evm_fact(
        EvmFact("ethereum", "IMPLEMENTATION_CHANGED", {}, "0xabc:7"), set()
    )

    assert result.evidence["source_url"] == "https://etherscan.io/tx/0xabc"


def test_snapshot_evidence_links_to_chain_explorer_block() -> None:
    result = evaluate_evm_fact(
        EvmFact("bsc", "PAUSED", {"block": 123}, "bsc:snapshot:123"), set()
    )

    assert result.evidence["source_url"] == "https://bscscan.com/block/123"
